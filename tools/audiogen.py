#!/usr/bin/env python3
"""Music generation runner for the K0B0L harness (MiniMax-Music3).

A thin, chatty CLI around the audio.cpp engine. The card must be free:
the swap supervisor stops the llm service before a run and the runner
hands it back when the file is written (or the run dies), mirroring
tools/imagegen.py's discipline with one difference -- audio.cpp is a
one-shot CLI, so there is no persistent music server to keep alive.

Swap protocol (http://127.0.0.1:7861, tools/gpu_swap_server.py):
    POST /swap/music   free the card for a music run (llama and sd stop)
    POST /swap/llm     bring the text models back up

Progress contract: "PHASE <text>" and "PROGRESS <n>/<m>" lines on
stderr, exactly like imagegen.py, so rt_harness tools decode the same
marker grammar for its hook events.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENGINE_DIR = Path(os.environ.get("K0B0L_AUDIO_DIR", "/home/jeff/audio.cpp"))
ENGINE_CLI = Path(os.environ.get(
    "K0B0L_AUDIO_BIN",
    str(ENGINE_DIR / "build" / "bin" / "audiocpp_cli")))
MODEL_DIR = Path(os.environ.get(
    "K0B0L_MUSIC_MODELS", "/mnt/k0b0l-models/MiniMax-Music3-GGUF"))
OUT_DIR = Path(os.environ.get(
    "K0B0L_MUSIC_DIR", str(Path(__file__).resolve().parent.parent / "audio")))
SWAP_API = os.environ.get("K0B0L_SWAP_API", "http://127.0.0.1:7861")

# Measured on the 1070 (Q4_0 LM): 10 s of audio = 95 s wall, 30 s =
# 315 s wall. RTF ~10, mostly the AR stage; a safety factor covers the
# flow/vocoder tail and NTFS weight loads.
WALL_PER_AUDIO_SEC = 12.0
MIN_WALL = 240.0
# VRAM ceiling, measured not guessed: free after the 5729 MiB q4_0 LM
# and compute workspace ~1.92 GiB; KV is 0.281 MiB/step (36 layers,
# F16, 8 kv-heads, CFG batch 2) at 25 frames/s, minus prompt steps.
# ~265 s of AR budget on a bare card; 240 leaves margin for prompt
# length and the flow/vocoder tail. duration_sec is an AR frame
# budget, not a hard waveform cap -- the rendered clip can run longer.
MAX_DURATION = 240
MAX_STEPS = 60


def _post(path: str, payload: bytes, timeout: int) -> tuple[bool, str]:
    request = urllib.request.Request(
        SWAP_API.rstrip("/") + path, data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status == 200, body
    except urllib.error.HTTPError as exc:
        return False, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - report, never crash
        return False, str(exc)


def swap_available() -> bool:
    try:
        with urllib.request.urlopen(SWAP_API.rstrip("/") + "/state",
                                    timeout=2) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def free_the_card() -> bool:
    ok, body = _post("/swap/music", b"{}", timeout=200)
    if not ok:
        print(f"swap supervisor refused: {body}", file=sys.stderr)
        return False
    return True


def swap_back_to_llm() -> None:
    """Return the card to the text models; best-effort, never raises."""
    try:
        _post("/swap/llm", b"{}", timeout=140)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not swap back to the llm service: {exc}",
              file=sys.stderr)


PID_FILE = Path(os.environ.get(
    "K0B0L_MUSIC_PID", "/tmp/k0b0l-music-engine.pid"))


def _register_engine(pid: int) -> None:
    try:
        PID_FILE.write_text(str(pid))
    except OSError:
        pass


def _unregister_engine() -> None:
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def _phase(text: str) -> None:
    print(f"PHASE {text}", file=sys.stderr, flush=True)


def _progress(current: int, total: int) -> None:
    print(f"PROGRESS {current}/{total}", file=sys.stderr, flush=True)


def generate(caption: str, out_path: Path, lyrics: str = "",
             duration: int = 0, steps: int = 0, seed: int = 0) -> bool:
    """Run one generation; returns True iff ``out_path`` exists after."""
    if not ENGINE_CLI.is_file():
        print(f"music engine not built: {ENGINE_CLI}", file=sys.stderr)
        return False
    if not MODEL_DIR.is_dir():
        print(f"music model files missing: {MODEL_DIR}", file=sys.stderr)
        return False
    duration = max(1, min(int(duration or 30), MAX_DURATION))
    steps = max(0, min(int(steps or 30), MAX_STEPS))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not free_the_card():
        return False
    _phase("loading MiniMax-Music3 weights")
    started = time.monotonic()
    try:
        # Stream the engine's own --log lines through as phase markers.
        # audio.cpp prints its timing/progress stream to stdout with
        # --log; errors land on stderr. Both pipes drained select-style
        # so a silent NTFS weight load still hits the deadline.
        argv = [
            str(ENGINE_CLI), "--task", "gen", "--family", "minimax_music3",
            "--model", str(MODEL_DIR), "--backend", "cuda", "--log",
            "--text", caption or "instrumental music",
            "--request-option", f"duration_sec={duration}",
            "--out", str(out_path),
        ]
        if lyrics:
            argv += ["--request-option", f"lyrics={lyrics}"]
        else:
            # The engine is lyrics-conditioned: it refuses a run with
            # none (session.cpp: "MiniMax Music 3 requires lyrics").
            # [inst] is the structural tag for an instrumental take --
            # verified on-card: it generates a clean instrumental clip.
            argv += ["--request-option", "lyrics=[inst]"]
        if steps:
            argv += ["--request-option", f"num_inference_steps={steps}"]
        if seed:
            argv += ["--request-option", f"seed={seed}"]
        env = dict(os.environ, GGML_CUDA_NO_VMM="1")

        process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=str(ENGINE_DIR))
        assert process.stdout is not None and process.stderr is not None
        _register_engine(process.pid)
        tail: list[str] = []
        deadline = time.monotonic() + MIN_WALL + WALL_PER_AUDIO_SEC * duration
        import selectors

        sel = selectors.DefaultSelector()
        sel.register(process.stdout, selectors.EVENT_READ)
        sel.register(process.stderr, selectors.EVENT_READ)
        try:
            while True:
                events = sel.select(timeout=1.0)
                for key, _ in events:
                    stream = key.fileobj
                    if not isinstance(stream, io.TextIOBase):
                        continue
                    line = stream.readline()
                    if not line:
                        continue
                    clean = line.rstrip()
                    if stream is process.stderr:
                        tail.append(clean[:200])
                        if len(tail) > 40:
                            tail.pop(0)
                    else:
                        # stdout carries the framework's timing/progress
                        # stream; forward the interesting lines as
                        # phases and swallow the rest.
                        if any(word in clean for word in
                               ("phase", "stage", "progress", "loaded")):
                            _phase(clean[:120])
                if process.poll() is not None:
                    for stream in (process.stdout, process.stderr):
                        for line in stream:
                            pass
                    break
                if time.monotonic() > deadline:
                    process.kill()
                    process.wait()
                    print("generation exceeded its wall-clock budget",
                          file=sys.stderr)
                    return False
        finally:
            sel.close()
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        if process.returncode != 0:
            print(f"music engine exited {process.returncode}; "
                  f"last output: {' | '.join(tail[-3:])}", file=sys.stderr)
            return False
    except OSError as exc:
        print(f"could not run the music engine: {exc}", file=sys.stderr)
        return False
    finally:
        _unregister_engine()
        swap_back_to_llm()
    if not out_path.is_file():
        print(f"the engine finished without writing {out_path}",
              file=sys.stderr)
        return False
    _phase(f"done in {time.monotonic() - started:.0f}s")
    return True


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("gen", "swapfree", "swapback", "state"):
        print("usage: audiogen.py gen <caption> [--lyrics TEXT] "
              "[--duration N] [--steps N] [--seed N] [--out PATH] | "
              "swapfree | swapback | state", file=sys.stderr)
        return 2
    if argv[0] == "swapback":
        swap_back_to_llm()
        print("swap-back requested", file=sys.stderr)
        return 0
    if argv[0] == "swapfree":
        ok, body = _post("/swap/music", b"{}", timeout=200)
        if not ok:
            print(f"swap supervisor refused: {body}", file=sys.stderr)
            return 1
        print("swap-free requested", file=sys.stderr)
        return 0
    if argv[0] == "state":
        try:
            with urllib.request.urlopen(SWAP_API.rstrip("/") + "/state",
                                        timeout=2) as response:
                print(response.read().decode())
                return 0
        except Exception as exc:  # noqa: BLE001
            print(f"swap supervisor unreachable: {exc}", file=sys.stderr)
            return 1
    caption = argv[1] if len(argv) > 1 else ""
    lyrics = ""
    duration = 0
    steps = 0
    seed = 0
    out = OUT_DIR / f"music-{int(time.time())}.wav"
    i = 2
    while i < len(argv):
        flag, value = argv[i], (argv[i + 1] if i + 1 < len(argv) else "")
        if flag == "--lyrics":
            lyrics = value
            i += 2
        elif flag == "--duration":
            duration = int(value or 0)
            i += 2
        elif flag == "--steps":
            steps = int(value or 0)
            i += 2
        elif flag == "--seed":
            seed = int(value or 0)
            i += 2
        elif flag == "--out":
            out = Path(value)
            i += 2
        else:
            print(f"unknown flag: {flag}", file=sys.stderr)
            return 2
    ok = generate(caption, out, lyrics=lyrics, duration=duration,
                  steps=steps, seed=seed)
    if not ok:
        return 1
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
