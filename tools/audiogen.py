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
HQ_MODEL_DIR = Path(os.environ.get(
    "K0B0L_MUSIC_HQ_DIR", "/home/jeff/models/minimax-hq"))
OUT_DIR = Path(os.environ.get(
    "K0B0L_MUSIC_DIR", str(Path(__file__).resolve().parent.parent / "audio")))
SWAP_API = os.environ.get("K0B0L_SWAP_API", "http://127.0.0.1:7861")

# Measured on the 1070 (Q4_0 LM): the 120 s / 50-step run took 1944 s
# wall = ~16 s of wall per second of audio (AR dominates; flow/vocoder
# tail adds proportional time). 20 keeps margin over that for NTFS
# weight loads and the longer AR frames at big durations.
WALL_PER_AUDIO_SEC = 20.0
# Flow stage wall scales with steps: measured 120 s @ 80 steps blew the
# flat budget at ~58 min (46-min budget for ~30-step pacing). Extra
# steps add roughly (steps - 30) * 0.45 s of flow wall per second of
# audio, so the budget must scale or high-step runs die mid-render.
WALL_PER_AUDIO_SEC_PER_STEP = 0.45
# HQ upgrades only the flow transformer (q4_0 -> q8_0, ~1.9x bytes) and
# depth decoder (q8_0 -> bf16, ~1.8x): the AR stage and its LM are
# unchanged, so HQ scales the STEP (flow) term only, not the whole
# wall. 0.45 is already ~2.3x the measured stock lower bound
# (wall(80)>2640s vs wall(50)=1944s => >=0.19 s/s/step), and the same
# margin is kept for HQ at 1.9x.
WALL_HQ_STEP_FACTOR = 1.9
MIN_WALL = 240.0
# VRAM ceiling, measured not guessed: free after the 5729 MiB q4_0 LM
# and compute workspace ~1.92 GiB; KV is 0.281 MiB/step (36 layers,
# F16, 8 kv-heads, CFG batch 2) at 25 frames/s, minus prompt steps.
# ~265 s of AR budget on a bare card; 240 leaves margin for prompt
# length and the flow/vocoder tail. duration_sec is an AR frame
# budget, not a hard waveform cap -- the rendered clip can run longer.
MAX_DURATION = 240
MAX_STEPS = 100

# Adaptive KV budget (the HF low-VRAM recipe, translated for a
# one-process engine with no layer offloading): the decode graph holds
# (prompt_steps + duration x 25) KV steps at 0.281 MiB/step next to the
# 5729 MiB weights. Long captions+lyrics EAT that budget from the
# front: the failing 240 s run with full structured lyrics needed
# ~1906 MiB and OOM'd. The runner clamps duration so the whole
# prompt+frames allocation stays inside what the card has left.
KV_MI_PER_STEP = 0.281          # measured: 36 L, F16, 8 kvh, CFG x2
# Calibrated from the two measured endpoints on this card (weights are
# DEVICE-resident: an HQ smoke run proved it by failing to cudaMalloc
# the full 9.4 GiB q8_0 LM buffer):
#   PASS: 120 s user prompt, ~680 est prompt steps -> decode alloc
#         (680 + 3000) x 0.281 = 1030 MiB, ran clean to a 119.8 s clip.
#   FAIL: skill prompt (59w caption + 297w lyrics -> ~267 est steps),
#         duration 240 -> (267 + 6000) x 0.281 = 1908 MiB, the logged
#         "allocating 1906.58 MiB ... out of memory".
# Headroom therefore sits between those; 1300 keeps a real margin under
# the failing point while allowing everything that has ever passed.
# (The mid-run nvidia-smi reading of ~1.7 GiB during a stock 120 s run
# is the FLOW phase -- mem_saver has already released the AR weights;
# it is NOT evidence of host-streamed weights.)
KV_HEADROOM_MI = 1300.0
# q8_0 KV cache halves the per-step cost (0.281 -> 0.141 MiB) with a
# small quality margin and ~10-15% AR speed cost -- the standard
# llama.cpp recipe for 8 GB cards. The runner switches to it whenever
# the f16 budget cannot cover the asked duration, so full-length songs
# (240 s) generate regardless of prompt size instead of clamping.
KV_MI_PER_STEP_Q8 = 0.141
WORDS_TO_PROMPT_STEPS = 0.75    # est tokenized steps per word of
                                # caption+lyrics (engine caps at 5000)
MAX_PROMPT_STEPS = 5000         # engine hard cap (HF README)


def _kv_duration_cap(caption: str, lyrics: str,
                     kv_mi_per_step: float = KV_MI_PER_STEP) -> int:
    """The largest duration (seconds) whose decode KV fits beside the
    weights, given the prompt this run will tokenize."""
    words = len((caption or "").split()) + len((lyrics or "").split())
    prompt_steps = min(int(words * WORDS_TO_PROMPT_STEPS),
                      MAX_PROMPT_STEPS)
    steps_free = max(0, int(KV_HEADROOM_MI / kv_mi_per_step) - prompt_steps)
    return max(1, steps_free // 25)


# High-quality component set for an 8 GB card. MEASURED (the failed
# smoke run): the engine cudaMalloc's the FULL language-model weight
# buffer on device -- a 9.4 GiB q8_0 LM can never fit. So HQ keeps the
# stock q4_0 LM (the AR semantic stage) and upgrades only the stages
# the step knob actually drives: the flow transformer q4_0 -> q8_0
# (1.40 -> 2.61 GB, both device-resident during the flow phase beside
# ~1.9 GiB compute) and the RVQ depth decoder q8_0 -> bf16
# (0.71 -> 1.29 GB). This is where "more steps changes nothing"
# actually lives: a q4_0 denoiser saturates no matter the Euler count.
HQ_TRANSFORMER = "transformer_q8_0.gguf"
HQ_DEPTH = "rvq_depth_decoder_bf16.gguf"


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
             duration: int = 0, steps: int = 0, seed: int = 0,
             decode_graph_arena_mb: int = 0, guidance_scale: float = 0,
             ar_guidance_scale: float = 0, top_k: int = 0,
             flow_uncond_interval: int = 0, flow_uncond_warmup: int = 0,
             quality: str = "") -> bool:
    """Run one generation; returns True iff ``out_path`` exists after."""
    if not ENGINE_CLI.is_file():
        print(f"music engine not built: {ENGINE_CLI}", file=sys.stderr)
        return False
    model_dir = MODEL_DIR
    session_options: list[tuple[str, str]] = []
    hq = (quality or "").strip().lower() in ("hq", "high", "highq")
    if hq:
        wanted = {
            "minimax_music3.flow_transformer_gguf": HQ_TRANSFORMER,
            "minimax_music3.rvq_depth_decoder_gguf": HQ_DEPTH,
        }
        if all((HQ_MODEL_DIR / name).is_file() for name in wanted.values()):
            model_dir = HQ_MODEL_DIR
            session_options = list(wanted.items())
            print("PHASE hq components: q8_0 flow transformer + bf16 "
                  "depth decoder over the stock q4_0 lm", file=sys.stderr,
                  flush=True)
        else:
            missing = [name for name in wanted.values()
                       if not (HQ_MODEL_DIR / name).is_file()]
            print(f"PHASE hq components missing ({', '.join(missing)}); "
                  "falling back to the stock q4_0 set", file=sys.stderr,
                  flush=True)
    if not model_dir.is_dir():
        print(f"music model files missing: {model_dir}", file=sys.stderr)
        return False
    duration = max(1, min(int(duration or 30), MAX_DURATION))
    steps = max(0, min(int(steps or 30), MAX_STEPS))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Adaptive KV budget: the engine allocates the FULL decode KV
    # (prompt + duration x 25 frames) up front. If f16 cannot cover the
    # asked duration for this prompt, switch to q8_0 KV (half the bytes,
    # small quality margin) BEFORE clamping -- a full-length song beats
    # a truncated one. Only when even q8_0 cannot cover it does the
    # duration clamp (extremely long prompts at 240 s).
    kv_cache_type = "f16"
    if duration > _kv_duration_cap(caption or "", lyrics or ""):
        kv_cache_type = "q8_0"
        print(f"PHASE switching to q8_0 KV cache: f16 cannot hold "
              f"{duration}s for this prompt size (q8_0 halves KV "
              f"VRAM; small quality margin)", file=sys.stderr,
              flush=True)
    kv_cap = _kv_duration_cap(caption or "", lyrics or "",
                              KV_MI_PER_STEP_Q8
                              if kv_cache_type == "q8_0"
                              else KV_MI_PER_STEP)
    if duration > kv_cap:
        print(f"PHASE duration {duration}s still exceeds the KV budget "
              f"even at q8_0; clamping to {kv_cap}s", file=sys.stderr,
              flush=True)
        duration = kv_cap

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
            "--model", str(model_dir), "--backend", "cuda", "--log",
            "--session-option", f"minimax_music3.kv_cache_type={kv_cache_type}",
            "--text", caption or "instrumental music",
            "--request-option", f"duration_sec={duration}",
            "--out", str(out_path),
        ]
        for opt_name, opt_value in session_options:
            argv += ["--session-option", f"{opt_name}={opt_value}"]
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
        if guidance_scale:
            argv += ["--request-option", f"guidance_scale={guidance_scale}"]
        if ar_guidance_scale:
            argv += ["--request-option", f"ar_guidance_scale={ar_guidance_scale}"]
        if top_k:
            argv += ["--request-option", f"top_k={top_k}"]
        if flow_uncond_interval:
            argv += ["--request-option", f"flow_uncond_interval={flow_uncond_interval}"]
        if flow_uncond_warmup:
            argv += ["--request-option", f"flow_uncond_warmup={flow_uncond_warmup}"]
        if decode_graph_arena_mb:
            argv += ["--request-option", f"minimax_music3.decode_graph_arena_mb={decode_graph_arena_mb}"]
        env = dict(os.environ, GGML_CUDA_NO_VMM="1")

        process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=str(ENGINE_DIR))
        assert process.stdout is not None and process.stderr is not None
        _register_engine(process.pid)
        tail: list[str] = []
        step_wall = max(0, steps - 30) * WALL_PER_AUDIO_SEC_PER_STEP * duration
        if hq:
            step_wall *= WALL_HQ_STEP_FACTOR
        wall = MIN_WALL + WALL_PER_AUDIO_SEC * duration + step_wall
        deadline = time.monotonic() + wall
        import selectors

        sel = selectors.DefaultSelector()
        sel.register(process.stdout, selectors.EVENT_READ)
        sel.register(process.stderr, selectors.EVENT_READ)
        # Heartbeat: the engine is silent for the whole multi-minute AR
        # decode (a 120 s request runs 20+ min with zero log lines), and
        # silence on the wire is indistinguishable from a stall. A phase
        # marker every 15 s keeps the UI's elapsed/working label visibly
        # alive and proves the runner loop itself is healthy.
        last_beat = time.monotonic()
        beat_index = 0
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
                    last_beat = time.monotonic()
                if process.poll() is not None:
                    for stream in (process.stdout, process.stderr):
                        for line in stream:
                            pass
                    break
                if time.monotonic() - last_beat >= 15.0:
                    beat_index += 1
                    elapsed = time.monotonic() - started
                    _phase(f"generating · {elapsed:.0f}s elapsed"
                           f"{' · still working' if beat_index > 1 else ''}")
                    last_beat = time.monotonic()
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
              "[--duration N] [--steps N] [--seed N] [--out PATH] "
              "[--guidance-scale F] [--ar-guidance-scale F] [--top-k N] "
              "[--flow-uncond-interval N] [--flow-uncond-warmup N] "
              "[--decode-graph-arena-mb N] [--quality stock|hq] | "
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
    decode_graph_arena_mb = 0
    guidance_scale = 0
    ar_guidance_scale = 0
    top_k = 0
    flow_uncond_interval = 0
    flow_uncond_warmup = 0
    quality = ""
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
        elif flag == "--guidance-scale":
            guidance_scale = float(value or 0)
            i += 2
        elif flag == "--ar-guidance-scale":
            ar_guidance_scale = float(value or 0)
            i += 2
        elif flag == "--top-k":
            top_k = int(value or 0)
            i += 2
        elif flag == "--flow-uncond-interval":
            flow_uncond_interval = int(value or 0)
            i += 2
        elif flag == "--flow-uncond-warmup":
            flow_uncond_warmup = int(value or 0)
            i += 2
        elif flag == "--decode-graph-arena-mb":
            decode_graph_arena_mb = int(value or 0)
            i += 2
        elif flag == "--quality":
            quality = value
            i += 2
        elif flag == "--out":
            out = Path(value)
            i += 2
        else:
            print(f"unknown flag: {flag}", file=sys.stderr)
            return 2
    ok = generate(caption, out, lyrics=lyrics, duration=duration,
                  steps=steps, seed=seed, decode_graph_arena_mb=decode_graph_arena_mb,
                  guidance_scale=guidance_scale, ar_guidance_scale=ar_guidance_scale,
                  top_k=top_k, flow_uncond_interval=flow_uncond_interval,
                  flow_uncond_warmup=flow_uncond_warmup, quality=quality)
    if not ok:
        return 1
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
