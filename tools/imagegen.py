#!/usr/bin/env python3
"""Local uncensored image generation service for K0B0L.

Three models, all of which FULLY FIT the 8 GB GTX 1070 (weights on card,
text encoders on CPU):

  pony    Pony Diffusion V6 XL (SDXL)      ~6.5 GB   the fast everyday one
  qwen    Qwen-Image-2.1-UC Q4_0            ~4.8 GB   the uncensored Qwen
  chroma  Chroma 8.9B Q4_0 (Flux-class)     ~5.4 GB   the quality one

Usage:
  python3 imagegen.py list                    what is installed
  python3 imagegen.py gen MODEL "prompt" [--out PATH] [--size WxH]
      [--steps N] [--seed N] [--neg "negative prompt"]

The router's llama-server must NOT be running when generating (the two
services share the one GPU); imagegen refuses politely if it sees the
server's VRAM footprint. Output defaults to
$K0B0L_IMAGE_DIR (~/images), PNG, model-tagged filename.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

MODELS_DIR = Path(os.environ.get("K0B0L_IMAGE_MODELS", "/mnt/k0b0l-models"))
OUT_DIR = Path(os.environ.get("K0B0L_IMAGE_DIR",
                              Path.home() / "images"))

FILES = {
    "qwen": {
        "dit": "qwen-image-2.1-UC-Q4_0.gguf",
        "encoder": "qwen3vl_8b_heretic-Q4_K_M.gguf",
        "vae": "qwen_image_vae_2.1.safetensors",
    },
    "pony": {"ckpt": "ponyDiffusionV6XL_v6.safetensors"},
    "chroma": {
        "dit": "chroma-v44-unlocked-Q4_0.gguf",
        "encoder": "t5_xxl_fp8_e4m3fn.safetensors",
        "vae": "flux_vae.safetensors",
    },
}

#: Where the sd.cpp binaries live (built from leejet/stable-diffusion.cpp with
#: CUDA 12.5 + g++-12 for the Pascal card). Qwen and Chroma run through it
#: because their GGUF DiT files are sd.cpp's native format.
SD_BIN = Path(os.environ.get("K0B0L_SD_BIN", "/tmp/sd-cpp/build/bin/sd-cli"))


def list_installed() -> int:
    print(f"models dir: {MODELS_DIR}")
    print(f"output dir:  {OUT_DIR}")
    for name, parts in FILES.items():
        missing = [v for v in parts.values()
                   if not (MODELS_DIR / v).is_file()]
        state = "installed" if not missing else f"MISSING {', '.join(missing)}"
        print(f"  {name:8} {state}")
    return 0


def gpu_is_busy() -> bool:
    """The llama-server holds ~6.6 GB; refuse to fight it for the card."""
    try:
        import torch

        free, total = torch.cuda.mem_get_info()
        return free < 6_000_000_000  # less than ~6 GB free means it's running
    except Exception:  # noqa: BLE001
        return False


#: One address drives everything. The operator answers the endpoint prompt
#: once (or sets API_URL); the image API and the swap supervisor are the
#: same GPU box, on fixed sibling ports -- 11434 for text, 7860 for
#: images, 7861 for swaps -- so they derive from the configured endpoint's
#: host. Explicit env wins when a setup ever splits them across boxes.
def _derive_from_endpoint(env_name: str, port: int) -> str:
    import os
    from urllib.parse import urlparse

    explicit = os.environ.get(env_name)
    if explicit:
        return explicit
    # The configured LLM endpoint names the GPU box; reuse its host.
    try:
        from rt_harness.config import Config

        # None = read the live environment (API_URL, saved default, all of it)
        base = Config.from_env(None).api_url
    except ImportError:
        # Running as a plain script (tools/ is not a package): find the
        # repo root the way the rest of this file does.
        import sys as _sys
        from pathlib import Path as _Path

        _root = _Path(__file__).resolve().parent.parent
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        from rt_harness.config import Config

        base = Config.from_env(None).api_url
    except Exception:  # noqa: BLE001 - config problems surface elsewhere
        base = "http://127.0.0.1:11434/v1"
    host = urlparse(base).hostname or "127.0.0.1"
    return f"http://{host}:{port}"


#: The image API on the GPU box (sd.cpp's sd-server, OpenAI /v1/images).
#: Generation goes here first -- the harness may live on a different
#: machine than the GPU -- and falls back to the local subprocess only
#: when no server answers.
IMAGE_API = _derive_from_endpoint("K0B0L_IMAGE_API", 7860)

#: The swap supervisor (tools/gpu_swap_server.py): owns llama and
#: sd-server on the one card, swapping them on request. When the
#: image API is down but this answers, the client swaps, generates, and
#: swaps back -- the load/unload you'd otherwise do by hand.
SWAP_API = _derive_from_endpoint("K0B0L_SWAP_API", 7861)


def swap_progress() -> dict | None:
    """Live sampling progress from the supervisor, or None."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(SWAP_API.rstrip("/") + "/progress",
                                    timeout=2) as r:
            progress = json.loads(r.read()).get("progress")
            return progress if isinstance(progress, dict) else None
    except Exception:  # noqa: BLE001
        return None


def api_up(url: str = "") -> bool:
    """Is the image API answering? (Cheap probe; no GPU work.)"""
    import urllib.request

    base = (url or IMAGE_API).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/v1/models", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def swap_available() -> bool:
    """Is the swap supervisor answering on the GPU box?"""
    import urllib.request

    try:
        with urllib.request.urlopen(SWAP_API.rstrip("/") + "/state",
                                    timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def swap_to_image(model: str) -> bool:
    """Ask the supervisor to bring the image service up (llama goes down)."""
    import json
    import urllib.error
    import urllib.request

    payload = json.dumps({"model": model}).encode()
    request = urllib.request.Request(
        SWAP_API.rstrip("/") + "/swap/image", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=200) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception as exc:  # noqa: BLE001
        print(f"swap supervisor: {exc}", file=sys.stderr)
        return False


def swap_back_to_llm() -> None:
    """Return the card to the text models; best-effort, never raises."""
    import urllib.request

    try:
        request = urllib.request.Request(
            SWAP_API.rstrip("/") + "/swap/llm", data=b"{}",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=140)
    except Exception:  # noqa: BLE001
        print("warning: could not swap back to the llm service",
              file=sys.stderr)


def gen_via_api(model: str, prompt: str, out_path: Path, size: str,
                steps: int, seed: int, neg: str) -> bool:
    """Generate through the image API (POST /v1/images/generations).

    Returns True and writes ``out_path`` on success; False (with the
    reason on stderr) otherwise. Native knobs -- model params like steps,
    seed, sampler -- ride inside the prompt via sd.cpp's
    ``<sd_cpp_extra_args>`` extension, the documented way for the OpenAI
    compatibility surface.
    """
    import base64
    import json
    import urllib.error
    import urllib.request

    extra: dict = {"sample_params": {}}
    if steps:
        extra["sample_params"]["sample_steps"] = int(steps)
    if seed:
        extra["sample_params"]["seed"] = int(seed)
    if neg:
        extra["sample_params"]["negative_prompt"] = neg
    body_prompt = prompt
    if extra["sample_params"]:
        body_prompt = (f"{prompt} <sd_cpp_extra_args>"
                       f"{json.dumps(extra)}</sd_cpp_extra_args>")
    payload = json.dumps({
        "prompt": body_prompt,
        "n": 1,
        "size": size,
        "output_format": "png",
    }).encode()
    # Sample while the request is open: sd.cpp streams no progress over
    # HTTP, so the supervisor's /progress (the sd-server's log) is polled
    # on a daemon thread and the freshest state is printed as PROGRESS /
    # PHASE markers for the harness's stream reader to pick up. A missing
    # supervisor means no markers -- generation just runs uninstrumented.
    state: dict = {"progress": None, "phase": None}

    def _poll() -> None:
        import threading

        def run() -> None:
            last_change = time.time()
            while not state["done"]:
                progress = swap_progress()
                if progress != state["progress"]:
                    state["progress"] = progress
                    last_change = time.time()
                    if progress:
                        print(f"PROGRESS {progress['step']}/{progress['total']}",
                              file=sys.stderr, flush=True)
                if progress:
                    phase = f"sampling {progress['step']}/{progress['total']}"
                    # sd.cpp block-buffers its per-step updates and the
                    # VAE decode after the last step writes no n/m lines,
                    # so a still bar is usually "almost done", not "dead".
                    if time.time() - last_change > 45:
                        phase = "finalizing (VAE decode / save)"
                else:
                    phase = "preparing (loading / text-encoding)"
                if phase != state["phase"]:
                    state["phase"] = phase
                    print(f"PHASE {phase}", file=sys.stderr, flush=True)
                time.sleep(2)

        threading.Thread(target=run, daemon=True).start()

    state["done"] = False
    if swap_available():
        _poll()
    request = urllib.request.Request(
        IMAGE_API.rstrip("/") + "/v1/images/generations",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        # 25 minutes: measured 910 s for qwen at 1024x1024 on the 1070;
        # the old 600 s socket timeout dropped a generation that finished.
        with urllib.request.urlopen(request, timeout=1500) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"image api: HTTP {exc.code}: {detail}", file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"image api unreachable: {exc}", file=sys.stderr)
        return False
    finally:
        state["done"] = True
    rows = data.get("data") or []
    if not rows or not rows[0].get("b64_json"):
        print("image api returned no image data", file=sys.stderr)
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(rows[0]["b64_json"]))
    return True


def _slug(text: str, width: int = 24) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.lower())
    return (out[:width]).strip("-") or "image"


def _swap_state() -> dict:
    """The supervisor's ``/state``, or an empty dict when it cannot answer."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(SWAP_API.rstrip("/") + "/state",
                                    timeout=2) as r:
            state = json.loads(r.read())
            return state if isinstance(state, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _resident_image_model() -> str:
    """Which model the supervisor says the image service has loaded.

    Empty when the supervisor cannot answer: an unknowable resident is
    assumed to match rather than paying a reload on a guess.
    """
    return str(_swap_state().get("image_model") or "")


def gen(model: str, prompt: str, out: str | None, size: str,
        steps: int, seed: int, neg: str) -> int:
    if model not in FILES:
        print(f"unknown model {model!r}: {'/'.join(FILES)}", file=sys.stderr)
        return 2

    width, height = (int(n) for n in size.lower().split("x"))
    out_path = Path(out).expanduser() if out else (
        OUT_DIR / f"{model}-{_slug(prompt)}-{int(time.time())}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # The API first: on this network the GPU box is the server and the
    # harness may run elsewhere entirely. When the image service is down
    # but the swap supervisor is up, the client swaps the card over,
    # generates, and swaps back -- the load/unload is a request, not a
    # chore. Everything below is the same-box, no-supervisor fallback.
    via_swap = False
    if not api_up() and swap_available():
        print(f"[{model}] swapping the GPU to image service "
              f"(llm pauses)...", file=sys.stderr)
        if swap_to_image(model):
            via_swap = True
        else:
            print("the swap supervisor could not start the image service",
                  file=sys.stderr)
    elif api_up() and swap_available():
        # sd.cpp serves whichever model is resident, not the one asked
        # for; the supervisor knows what it loaded (measured: a pony
        # request answered by a resident qwen ran 910 s). Load the
        # requested model when they differ, and hand the card back to
        # the llm afterwards -- the chat is the primary workload.
        resident = _resident_image_model()
        if resident and resident != model:
            print(f"[{model}] image service has {resident!r} resident; "
                  f"swapping to {model}...", file=sys.stderr)
            if swap_to_image(model):
                via_swap = True
            else:
                print(f"the swap supervisor could not load {model}; "
                      f"generating with {resident}", file=sys.stderr)
    if api_up():
        print(f"[{model}] via image api {IMAGE_API}...", file=sys.stderr)
        ok = gen_via_api(model, prompt, out_path, size, steps, seed, neg)
        # Return the card whenever the supervisor owns it and the llm is
        # down -- not only when *this* run swapped it over. The incident:
        # an sd-server left up by an earlier session answered this one,
        # via_swap stayed False, and nobody gave the card back, so every
        # later model round hit a dead endpoint.
        state = _swap_state()
        if via_swap or (state and not state.get("llm", True)):
            print("swapping the GPU back to the llm service...",
                  file=sys.stderr)
            swap_back_to_llm()
        if ok:
            print(f"saved {out_path}")
            return 0
        print("the image api failed; falling back to local generation",
              file=sys.stderr)
    elif via_swap:
        swap_back_to_llm()

    missing = [v for v in FILES[model].values()
               if not (MODELS_DIR / v).is_file()]
    if missing:
        print(f"{model} is not fully downloaded here: {', '.join(missing)}",
              file=sys.stderr)
        return 2
    if gpu_is_busy():
        print("the GPU looks occupied (llama-server running?): stop it "
              "before generating, or start the image api server "
              "(rt-image-server.sh) and retry", file=sys.stderr)
        return 3

    started = time.time()
    print(f"[{model}] loading...", file=sys.stderr)
    import torch

    torch_dtype = torch.bfloat16

    if model == "pony":
        from diffusers import StableDiffusionXLPipeline

        pipe = StableDiffusionXLPipeline.from_single_file(
            str(MODELS_DIR / FILES["pony"]["ckpt"]),
            torch_dtype=torch_dtype,
            variant="fp16",
        )
        # The 8 GB Pascal card cannot hold all of SDXL (~7 GB bf16 weights)
        # plus activations. Model-cpu-offload keeps the text encoders in
        # system RAM (encode once per prompt, cheap) and moves only the
        # active component — UNet sampling, then VAE decode — to the GPU.
        # Slicing + tiling shave the activation peaks. Cost: speed.
        pipe.enable_model_cpu_offload()
        pipe.enable_attention_slicing()
        pipe.enable_vae_tiling()
        pipe.set_progress_bar_config(disable=True)
        print(f"[pony] generating {width}x{height} x {steps} steps...",
              file=sys.stderr)
        image = pipe(
            prompt=prompt,
            negative_prompt=neg or "low quality, worst quality, "
                                   "deformed, watermark",
            width=width, height=height,
            num_inference_steps=steps or 28,
            guidance_scale=7.0,
            generator=torch.Generator("cuda").manual_seed(seed),
        ).images[0]
    elif model == "qwen":
        # Proven live recipe (2026-10-06): sd.cpp with the Heretic GGUF
        # encoder via --llm, encoder + VAE on CPU (--backend clip=cpu,vae=cpu),
        # DiT on the GPU, --max-vram 6. ~11.5 s/step at 768x768.
        if not SD_BIN.is_file():
            print(f"sd-cli not found at {SD_BIN} (K0B0L_SD_BIN to override)",
                  file=sys.stderr)
            return 2
        import subprocess

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(SD_BIN),
            "--diffusion-model", str(MODELS_DIR / FILES["qwen"]["dit"]),
            "--llm", str(MODELS_DIR / FILES["qwen"]["encoder"]),
            "--vae", str(MODELS_DIR / FILES["qwen"]["vae"]),
            "-p", prompt,
            "--steps", str(steps or 20),
            "--sampling-method", "euler",
            "-W", str(width), "-H", str(height),
            "--seed", str(seed),
            "--offload-to-cpu",
            "--backend", "clip=cpu,vae=cpu",
            "--max-vram", "6",
            "-o", str(out_path),
        ]
        print(f"[qwen] sd.cpp: {width}x{height} x {steps or 20} steps...",
              file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr[-1500:], file=sys.stderr)
            return 1
        print(f"saved {out_path}")
        return 0
    else:
        # chroma: the same sd.cpp path, T5 encoder + Flux VAE.
        if not SD_BIN.is_file():
            print(f"sd-cli not found at {SD_BIN} (K0B0L_SD_BIN to override)",
                  file=sys.stderr)
            return 2
        import subprocess

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(SD_BIN),
            "--diffusion-model", str(MODELS_DIR / FILES["chroma"]["dit"]),
            "--t5xxl", str(MODELS_DIR / FILES["chroma"]["encoder"]),
            "--vae", str(MODELS_DIR / FILES["chroma"]["vae"]),
            "-p", prompt,
            "--steps", str(steps or 24),
            "--sampling-method", "euler",
            "-W", str(width), "-H", str(height),
            "--seed", str(seed),
            "--offload-to-cpu",
            "--backend", "clip=cpu,vae=cpu",
            "--max-vram", "6",
            "-o", str(out_path),
        ]
        print(f"[chroma] sd.cpp: {width}x{height} x {steps or 24} steps...",
              file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr[-1500:], file=sys.stderr)
            return 1
        print(f"saved {out_path}")
        return 0

    image.save(out_path)
    seconds = time.time() - started
    print(f"saved {out_path} ({seconds:.0f}s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show what is installed")
    sub.add_parser("swapback", help="hand the GPU back to the llm service")
    g = sub.add_parser("gen", help="generate one image")
    g.add_argument("model", choices=list(FILES))
    g.add_argument("prompt")
    g.add_argument("--out", default=None)
    g.add_argument("--size", default="1024x1024")
    g.add_argument("--steps", type=int, default=0)
    g.add_argument("--seed", type=int, default=int(time.time()))
    g.add_argument("--neg", default="")
    args = ap.parse_args()
    if args.cmd == "list":
        return list_installed()
    if args.cmd == "swapback":
        if not swap_available():
            print("no swap supervisor answering", file=sys.stderr)
            return 1
        swap_back_to_llm()
        print("swapped back to the llm service")
        return 0
    return gen(args.model, args.prompt, args.out, args.size,
               args.steps, args.seed, args.neg)


if __name__ == "__main__":
    sys.exit(main())
