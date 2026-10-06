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
import signal
import subprocess
import sys
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
    subprocess.run(["pkill", "-f", "sd-server"], check=False)
    for _ in range(20):
        if not image_healthy():
            return
        time.sleep(0.5)
    subprocess.run(["pkill", "-9", "-f", "sd-server"], check=False)
    time.sleep(1)


def start_llama() -> None:
    log = open(LLAMA_LOG, "ab")
    subprocess.Popen(
        ["./llama-server", "--models-dir", "./models",
         "--models-preset", "./models.ini", "--models-max", "1",
         "--host", "0.0.0.0", "--port", "11434"],
        cwd=str(LLAMA_DIR), stdout=log, stderr=log,
        start_new_session=True)


def start_sd(model: str) -> None:
    log = open(SD_LOG, "ab")
    subprocess.Popen(
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
                "image_models": list(IMAGE_MODELS),
            })
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
