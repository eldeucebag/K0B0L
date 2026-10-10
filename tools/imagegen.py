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
            seen_steps = False
            while not state["done"]:
                progress = swap_progress()
                if progress:
                    seen_steps = True
                if progress != state["progress"]:
                    state["progress"] = progress
                    last_change = time.time()
                    if progress:
                        print(f"PROGRESS {progress['step']}/{progress['total']}",
                              file=sys.stderr, flush=True)
                # Phases only for states the steps cannot express: the
                # label before the first step, the verdict when the steps
                # stop. A phase per step double-emits on the wire and the
                # total=0 frame flips the page's bar back to indeterminate
                # after every step -- a bar that flickers instead of fills.
                if progress and time.time() - last_change > 45:
                    # sd.cpp block-buffers its per-step updates: a still
                    # bar is usually "almost done", not "dead".
                    phase = "finalizing (VAE decode / save)"
                elif progress:
                    phase = None
                elif seen_steps:
                    # Steps ran, then the progress went away: the
                    # completion marker, between the last step and the
                    # HTTP response. Not "preparing" -- that would be a
                    # lie told at the one moment the operator is
                    # watching hardest.
                    phase = "finalizing (VAE decode / save)"
                else:
                    phase = "preparing (loading / text-encoding)"
                if phase != state["phase"]:
                    state["phase"] = phase
                    if phase:
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


def _start_progress_ticker() -> dict:
    """The shared progress state + its polling thread, per gen_via_api.

    ``done`` flips True when the HTTP request closes; the daemon prints
    PROGRESS n/m and PHASE lines to stderr while it waits, which the
    harness's stream reader turns into gen_progress hook events.
    """
    import threading

    state: dict = {"progress": None, "phase": None, "done": False}
    if swap_available():
        threading.Thread(target=_ticker_body, args=(state,),
                         daemon=True).start()
    return state


def _ticker_body(state: dict) -> None:
    last_change = time.time()
    seen_steps = False
    while not state["done"]:
        progress = swap_progress()
        if progress:
            seen_steps = True
        if progress != state["progress"]:
            state["progress"] = progress
            last_change = time.time()
            if progress:
                print(f"PROGRESS {progress['step']}/{progress['total']}",
                      file=sys.stderr, flush=True)
        if progress and time.time() - last_change > 45:
            phase = "finalizing (VAE decode / save)"
        elif progress:
            phase = None
        elif seen_steps:
            phase = "finalizing (VAE decode / save)"
        else:
            phase = "preparing (loading / text-encoding)"
        if phase != state["phase"]:
            state["phase"] = phase
            if phase:
                print(f"PHASE {phase}", file=sys.stderr, flush=True)
        time.sleep(2)


def gen_advanced(model: str, prompt: str, out: str,
                 negative_prompt: str = "", width: int = 1024,
                 height: int = 1024, steps: int = 0, seed: int = -1,
                 sampler: str = "", scheduler: str = "", rng: str = "",
                 cfg: float = 0.0, eta: float | None = None,
                 batch_count: int = 1, init_image: str | None = None,
                 mask_image: str | None = None,
                 denoise_strength: float | None = None,
                 mask_invert: bool = False,
                 custom_sigmas: list | None = None) -> bool:
    """Full-control generation: txt2img / img2img / inpaint over sdapi.

    The operator-facing surface ComfyUI/A1111 expose, mapped onto
    sd.cpp's own API: AUTOMATIC1111-shaped ``/sdapi/v1/txt2img`` or
    ``/img2img`` (init image, mask, denoising strength, sampler_name,
    scheduler, cfg_scale, batch), with the OpenAI-shaped
    ``/v1/images/generations`` as the fallback for older sd-server
    builds (its native knobs ride inside the prompt, same as
    :func:`gen_via_api`).

    ``rng``: the server's --rng is a STARTUP flag (std_default, cuda,
    cpu) and the sdapi body carries no equivalent; the only
    request-body-reachable RNG knob is ``brownian_tree_rng`` inside
    ``extra_sample_args``, which applies only to the brownian-tree
    noise sampler of noise-injecting samplers (e.g. dpm++2m_sde_bt).
    The parameter is kept for the day a body field exists and wired
    into the extra-args payload under that name; until then choosing
    it here changes nothing for plain samplers, by design -- a silent
    no-op is what the server itself would do with an unknown key.

    Returns True and writes ``out`` (batch > 1: ``out-N.png``) on
    success; False with the reason on stderr otherwise. The GPU swap
    chain follows gen()'s rule exactly: the card must end on the llm
    service, whoever swapped it over.
    """
    import base64
    import json
    import urllib.error
    import urllib.request
    from pathlib import Path as _P

    if model not in FILES:
        print(f"unknown model {model!r}: {'/'.join(FILES)}", file=sys.stderr)
        return False
    out_path = _P(out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    init_b64 = mask_b64 = ""
    if init_image:
        try:
            raw = _P(init_image).read_bytes()
            init_b64 = "data:image/png;base64," + base64.b64encode(raw).decode()
        except OSError as exc:
            print(f"could not read init image {init_image}: {exc}",
                  file=sys.stderr)
            return False
    if mask_image:
        try:
            raw = _P(mask_image).read_bytes()
            mask_b64 = "data:image/png;base64," + base64.b64encode(raw).decode()
        except OSError as exc:
            print(f"could not read mask image {mask_image}: {exc}",
                  file=sys.stderr)
            return False

    # -- the swap chain, verbatim from gen() --------------------------------
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
        resident = _resident_image_model()
        if resident and resident != model:
            print(f"[{model}] image service has {resident!r} resident; "
                  f"swapping to {model}...", file=sys.stderr)
            if swap_to_image(model):
                via_swap = True
            else:
                print(f"the swap supervisor could not load {model}; "
                      f"generating with {resident}", file=sys.stderr)

    def swap_back_if_ours() -> None:
        state = _swap_state()
        if via_swap or (state and not state.get("llm", True)):
            print("swapping the GPU back to the llm service...",
                  file=sys.stderr)
            swap_back_to_llm()

    if not api_up():
        if via_swap:
            swap_back_if_ours()
        print("the image api is unreachable; img2img/inpaint need sd-server "
              "(the local diffusers fallback does not take these controls)",
              file=sys.stderr)
        return False

    # -- the request ---------------------------------------------------------
    ticker = _start_progress_ticker()
    try:
        body: dict = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": int(width) or 1024,
            "height": int(height) or 1024,
            "seed": int(seed),
            "batch_size": max(1, min(8, int(batch_count))),
        }
        if steps:
            body["steps"] = int(steps)
        if cfg:
            body["cfg_scale"] = float(cfg)
        if sampler:
            body["sampler_name"] = sampler
        if scheduler:
            body["scheduler"] = scheduler
        extra: dict = {"sample_params": {}, "extra_sample_args": {}}
        if eta is not None:
            extra["sample_params"]["eta"] = float(eta)
        if custom_sigmas:
            extra["sample_params"]["custom_sigmas"] = list(custom_sigmas)
        if rng:
            # the one body-reachable RNG knob; only meaningful for
            # noise-injecting samplers using the brownian-tree noise
            extra["extra_sample_args"]["brownian_tree_rng"] = rng
        if init_b64:
            body["init_images"] = [init_b64]
            body["denoising_strength"] = (
                0.75 if denoise_strength is None
                else max(0.0, min(1.0, float(denoise_strength))))
            if mask_b64:
                body["mask"] = mask_b64
                body["inpainting_mask_invert"] = 1 if mask_invert else 0
        if extra["sample_params"] or extra["extra_sample_args"]:
            import json as _json
            body["prompt"] = (
                prompt + " <sd_cpp_extra_args>"
                + _json.dumps(extra) + "</sd_cpp_extra_args>")

        route = "img2img" if init_b64 else "txt2img"
        url = IMAGE_API.rstrip("/") + f"/sdapi/v1/{route}"
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        print(f"[{model}] via sdapi /{route}...", file=sys.stderr)
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:
                data = json.loads(response.read())
            images = [b for b in (data.get("images") or [])
                      if isinstance(b, str)]
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                # older sd-server: fall back to the OpenAI-shaped route
                print("sdapi route absent; falling back to "
                      "/v1/images/generations", file=sys.stderr)
                v1 = {
                    "prompt": body["prompt"],
                    "n": body["batch_size"],
                    "size": f"{body['width']}x{body['height']}",
                    "output_format": "png",
                }
                request = urllib.request.Request(
                    IMAGE_API.rstrip("/") + "/v1/images/generations",
                    data=json.dumps(v1).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=1800) as response:
                    data = json.loads(response.read())
                rows = data.get("data") or []
                images = [str(r.get("b64_json") or "")
                          for r in rows if r.get("b64_json")]
            else:
                detail = exc.read().decode("utf-8", "replace")[:300]
                print(f"image api: HTTP {exc.code}: {detail}",
                      file=sys.stderr)
                swap_back_if_ours()
                return False
        except Exception as exc:  # noqa: BLE001
            print(f"image api unreachable: {exc}", file=sys.stderr)
            swap_back_if_ours()
            return False
    finally:
        ticker["done"] = True

    if not images:
        print("the image api returned no images", file=sys.stderr)
        swap_back_if_ours()
        return False

    written = 0
    stem = out_path.with_suffix("")
    for index, encoded in enumerate(images, start=1):
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            blob = base64.b64decode(encoded)
        except Exception as exc:  # noqa: BLE001
            print(f"batch item {index} was not base64: {exc}", file=sys.stderr)
            continue
        if len(blob) < 50:
            print(f"batch item {index} is suspiciously small; skipped",
                  file=sys.stderr)
            continue
        target = out_path if len(images) == 1 else \
            _P(f"{stem}-{index}.png")
        target.write_bytes(blob)
        print(f"saved {target}")
        written += 1

    swap_back_if_ours()
    return written > 0


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
