#!/usr/bin/env python3
"""The GPU-box swap supervisor: one card, two services, hot-swapped on demand.

Topology: this box serves text (llama-server :11434) AND images
(sd-server :7860) to harness clients elsewhere. One 8 GB card means
only one service can hold it at a time, so this supervisor owns both
processes and swaps them on request:

  GET  /state                which service is up, and what models exist
  POST /swap/image  {"model": "pony"}   stop llama, start sd-server
  POST /swap/llm             stop sd-server, start the llama router

Clients (the harness's generate_image path, /image, or a human with
curl) swap, use the service, and swap back when done. Each swap is a
model load (~10-30 s), the same price the llama router already pays
when it evicts models -- but paid once per switch, not per image.

Run on the GPU box:  python3 tools/gpu_swap_server.py   (port 7861)
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LLAMA_DIR = Path("/home/jeff/llama-prism-b10743-adfffbe")
IMAGE_SERVER = REPO / "rt-image-server.sh"
LLAMA_LOG = "/tmp/llama-router.log"
SD_LOG = "/tmp/sd-server.log"

LLAMA_URL = "http://127.0.0.1:11434"
IMAGE_URL = "http://127.0.0.1:7860"
IMAGE_MODELS = ("pony", "qwen", "chroma")

#: Which image model this supervisor last started; None when the image
#: service is down or was started out-of-band. Reported by /state so a
#: client can tell the resident model from the one it asked for.
current_image_model: str | None = None

#: The children this supervisor Popen'd. A Popen'd server that exits is a
#: zombie until someone waits on it -- an sd-server sat defunct with the
#: port already free, reading as a live service in every ps output.
_sd_process: subprocess.Popen | None = None
_llama_process: subprocess.Popen | None = None


def _reap(handle: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """Collect a child this supervisor spawned, if it is done dying.

    The callers only invoke this after pkill/health loops report the
    service down, so the wait returns immediately; a process that
    somehow survives its kill is left alone, still owned by its handle.
    """
    if handle is None:
        return
    try:
        handle.wait(timeout=timeout)
    except Exception:  # noqa: BLE001 - still running: nothing to collect
        pass

#: Swaps are serialized: two concurrent requests must not interleave the
#: stop/start cycles of two different models.
_STATE_LOCK = threading.Lock()

#: A sampling-progress line from sd.cpp ("12/20 - 4.89s/it"). The weight
#: load lines say MB/s, so requiring s/it tells the two apart.
_PROGRESS_RE = re.compile(r"(\d+)/(\d+)\s*-\s*[\d.]+s/it")

#: The line sd.cpp writes when a generation is DONE. Seeing it means the
#: last step is no longer live progress -- without this check a *new*
#: run opens with the previous run's final step as a phantom full bar.
_SD_DONE_RE = re.compile(r"generate_image completed in")

#: Forward-only scan state for the sd-server log: bytes already read, and
#: the freshest step seen among them. ``pos`` of None means not yet
#: baselined -- the first read skips to the file's end, because history
#: from before the supervisor started watching is not progress.
_SD_SCAN: dict = {"pos": None, "last": None}


def _reset_sd_scan() -> None:
    """Forget everything watched so far (a new image run is starting)."""
    _SD_SCAN["pos"] = None
    _SD_SCAN["last"] = None


def read_sd_progress() -> dict | None:
    """The newest sampling step the sd-server has written, or None.

    Forward-only: each poll scans just the bytes written since the last
    one, so a step is reported when it happens rather than replayed from
    history. The first read after startup baselines at the file's end --
    a fresh generation opened with a stale "1/49" and never corrected,
    when the whole tail was re-scanned every poll. A shrunk file
    (rotation) resets the baseline. sd.cpp block-buffers its per-step
    carriage-return updates, so steps can arrive in bursts; the last
    step of a burst is the freshest truth there is.
    """
    try:
        size = os.path.getsize(SD_LOG)
    except OSError:
        return None
    if _SD_SCAN["pos"] is None:  # first read: skip the history
        _SD_SCAN["pos"] = size
        return None
    if size < _SD_SCAN["pos"]:  # rotated or truncated: re-baseline
        _SD_SCAN["pos"] = size
        _SD_SCAN["last"] = None
    with open(SD_LOG, "rb") as handle:
        handle.seek(_SD_SCAN["pos"])
        chunk = handle.read().decode("utf-8", "replace")
        _SD_SCAN["pos"] += len(chunk.encode("utf-8", "replace"))
    last = None
    for match in _PROGRESS_RE.finditer(chunk):
        last = match
    if _SD_DONE_RE.search(chunk):
        # The run finished: its last step is history, not live progress.
        _SD_SCAN["last"] = None
        return None
    if last is not None:
        step, total = int(last.group(1)), int(last.group(2))
        if 0 < step <= total < 100_000:
            _SD_SCAN["last"] = {"step": step, "total": total}
    return _SD_SCAN["last"]


def healthy(url: str, path: str = "/health") -> bool:
    try:
        with urllib.request.urlopen(url + path, timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def image_healthy() -> bool:
    try:
        with urllib.request.urlopen(IMAGE_URL + "/v1/models", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def stop_llama() -> None:
    # The llama service is systemd-managed (k0b0l-llama, Restart=always):
    # a bare pkill would have systemd resurrect it mid-swap and re-take
    # the card while the image server is loading. Stop the unit itself;
    # fall back to pkill for non-systemd setups.
    subprocess.run(["sudo", "-n", "systemctl", "stop", "k0b0l-llama.service"],
                   check=False)
    if healthy(LLAMA_URL):
        subprocess.run(["pkill", "-f", "llama-server --models-dir"],
                       check=False)
    for _ in range(30):
        if not healthy(LLAMA_URL):
            return
        time.sleep(0.5)
    subprocess.run(["pkill", "-9", "-f", "llama-server --models-dir"],
                   check=False)
    time.sleep(1)


def stop_sd() -> None:
    global current_image_model, _sd_process
    subprocess.run(["pkill", "-f", "sd-server"], check=False)
    for _ in range(20):
        if not image_healthy():
            current_image_model = None
            _reap(_sd_process)
            _sd_process = None
            return
        time.sleep(0.5)
    subprocess.run(["pkill", "-9", "-f", "sd-server"], check=False)
    time.sleep(1)
    current_image_model = None
    _reap(_sd_process)
    _sd_process = None


def start_llama() -> None:
    # Prefer the systemd unit (k0b0l-llama) so the service manager owns
    # the process again; the direct spawn is the non-systemd fallback.
    global _llama_process
    stopped = subprocess.run(
        ["sudo", "-n", "systemctl", "start", "k0b0l-llama.service"],
        check=False)
    if stopped.returncode == 0:
        return
    log = open(LLAMA_LOG, "ab")
    _llama_process = subprocess.Popen(
        ["./llama-server", "--models-dir", "./models",
         "--models-preset", "./models.ini", "--models-max", "1",
         "--host", "0.0.0.0", "--port", "11434"],
        cwd=str(LLAMA_DIR), stdout=log, stderr=log,
        start_new_session=True)


def start_sd(model: str) -> None:
    global current_image_model, _sd_process
    current_image_model = model
    log = open(SD_LOG, "ab")
    _sd_process = subprocess.Popen(
        [str(IMAGE_SERVER), model],
        stdout=log, stderr=log, start_new_session=True)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/state":
            self._json(200, {
                "llm": healthy(LLAMA_URL),
                "image": image_healthy(),
                "image_model": current_image_model,
                "image_models": list(IMAGE_MODELS),
            })
        elif self.path == "/progress":
            # Live sampling steps from the sd-server's log; None when it
            # is not mid-sampling (loading, encoding, or no image service).
            self._json(200, {"progress": read_sd_progress()})
        else:
            self._json(404, {"error": "GET /state"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/swap/image":
            model = str(body.get("model") or "pony").lower()
            if model not in IMAGE_MODELS:
                self._json(400, {"error":
                                 f"model must be one of {IMAGE_MODELS}"})
                return
            with _STATE_LOCK:
                _reset_sd_scan()  # a new run: no phantom steps from the last one
                if image_healthy() and current_image_model == model:
                    # Already serving the requested model: a reload would
                    # be a minutes-long no-op on this card.
                    self._json(200, {"ok": True, "service": "image",
                                     "model": model, "url": IMAGE_URL})
                    return
                stop_llama()
                stop_sd()
                start_sd(model)
            deadline = time.time() + 180  # weight load from NTFS is slow
            while time.time() < deadline:
                if image_healthy():
                    self._json(200, {"ok": True, "service": "image",
                                     "model": model,
                                     "url": IMAGE_URL})
                    return
                time.sleep(1)
            self._json(503, {"ok": False,
                             "error": "image server did not come up; "
                                      f"see {SD_LOG}"})
        elif self.path == "/swap/llm":
            with _STATE_LOCK:
                stop_sd()
                stop_llama()
                start_llama()
            deadline = time.time() + 120
            while time.time() < deadline:
                if healthy(LLAMA_URL):
                    self._json(200, {"ok": True, "service": "llm",
                                     "url": LLAMA_URL})
                    return
                time.sleep(1)
            self._json(503, {"ok": False,
                             "error": f"llama did not come up; see {LLAMA_LOG}"})
        else:
            self._json(404, {"error": "POST /swap/image | /swap/llm"})

    def log_message(self, fmt, *args) -> None:  # quiet
        pass


def main() -> int:
    port = int(os.environ.get("K0B0L_SWAP_PORT", "7861"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"gpu swap supervisor on :{port} "
          f"(llm {LLAMA_URL} / image {IMAGE_URL})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
