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


def _slug(text: str, width: int = 24) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.lower())
    return (out[:width]).strip("-") or "image"


def gen(model: str, prompt: str, out: str | None, size: str,
        steps: int, seed: int, neg: str) -> int:
    if model not in FILES:
        print(f"unknown model {model!r}: {'/'.join(FILES)}", file=sys.stderr)
        return 2
    missing = [v for v in FILES[model].values()
               if not (MODELS_DIR / v).is_file()]
    if missing:
        print(f"{model} is not fully downloaded yet: {', '.join(missing)}",
              file=sys.stderr)
        return 2
    if gpu_is_busy():
        print("the GPU looks occupied (llama-server running?): stop it "
              "before generating (image and text services share the card)",
              file=sys.stderr)
        return 3

    import torch

    width, height = (int(n) for n in size.lower().split("x"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(out).expanduser() if out else (
        OUT_DIR / f"{model}-{_slug(prompt)}-{int(time.time())}.png")

    started = time.time()
    print(f"[{model}] loading...", file=sys.stderr)
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
    return gen(args.model, args.prompt, args.out, args.size,
               args.steps, args.seed, args.neg)


if __name__ == "__main__":
    sys.exit(main())
