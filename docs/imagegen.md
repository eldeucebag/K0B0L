# Image generation

K0B0L generates images locally, on the same GPU the text models use.
Three uncensored models ship, chosen so every one **fully fits the 8 GB
card** — no cloud, no API, nothing leaves the machine.

| name | what it is | speed (768²) | prompting |
| --- | --- | --- | --- |
| `pony` | Pony Diffusion V6 XL (SDXL) | ~1 minute | comma-separated tags work best |
| `qwen` | Qwen-Image-2.1-UC + Heretic text encoder | ~4 minutes | natural language, best adherence |
| `chroma` | Chroma 8.9B (uncensored Flux-class) | ~4 minutes | natural language |

Weights live in `/mnt/k0b0l-models` (26 GB); the runner is
`tools/imagegen.py`:

- **pony** runs through diffusers with model-cpu-offload, attention
  slicing and VAE tiling — the text encoders live in system RAM.
- **qwen** and **chroma** run through stable-diffusion.cpp (built for
  this card: CUDA 12.5, sm_61), with the text encoder and VAE on CPU
  (`--backend clip=cpu,vae=cpu`) and the diffusion model on the GPU.

## One GPU, two services

Image generation and the text models **share the card**, so a swap
supervisor owns it (`tools/gpu_swap_server.py`, port 7861; see
`docs/gpu-services`): generation **swaps the card automatically** —
llama stops, the image model loads, the image generates, and the card
returns to the LLM. If the supervisor is unreachable and the card is
busy, the runner still refuses with the reason:

> the GPU looks occupied (llama-server running?): stop it before
> generating

The one rule that matters: a run that strands the card must own its
return — if a generation times out or its requester dies, the runner's
`swapback` path and the supervisor's ownership check give the card back,
so later model rounds never hit a dead endpoint.

## Using it

**From the chat (the model):** it calls `generate_image(prompt, model=…)`
like any other tool. The image is saved under `images/` in the workspace
root and rendered into the transcript.

**From the keyboard (you):**

```
/image a lighthouse on a cliff at dusk            # pony, the fast one
/image qwen a neon alley in the rain             # qwen for adherence
/image chroma an astronaut tending a moon garden  # flux-class
/image                                           # opens the web UI's dialog
```

**Full control (txt2img / img2img / inpaint):** the web UI's 🎨 dialog —
upload an init image, paint an inpaint mask, and pick sampler,
scheduler, RNG source, seed, cfg, eta, batch (see `docs/webui`). The
same surface is scriptable:

```bash
python3 tools/imagegen.py gen-adv pony "tag one, tag two" \
    --steps 24 --seed 7 --sampler euler_a --scheduler karras
```

**From the shell (simple path):**

```bash
python3 tools/imagegen.py list
python3 tools/imagegen.py gen pony "tag one, tag two" --steps 24 --seed 7
```

Options: `--size WxH` (default 768x768 — 1024 needs headroom pony does
not have on this card), `--steps`, `--seed`, `--neg` (negative prompt),
`--out PATH`.

## Terminal display

Images render **in the transcript** in the Textual front end: sixel in
capable terminals, half-block characters elsewhere — an image is always
visible as an image, with its path beneath it. The plain front end
prints the saved path. The web UI shows the image itself, with copy /
retry / delete actions under it.

## Files

```
/mnt/k0b0l-models/          the weights (K0B0L_IMAGE_MODELS)
tools/imagegen.py           the runner (list | gen | gen-adv | swapback)
images/                     output, inside the chat workspace
```

Environment overrides: `K0B0L_IMAGE_MODELS` (weights dir),
`K0B0L_IMAGE_DIR` (default output), `K0B0L_SD_BIN` (sd.cpp binary,
default `/tmp/sd-cpp/build/bin/sd-cli` — move it somewhere permanent
before a reboot clears /tmp), `K0B0L_IMAGE_API` and `K0B0L_SWAP_API`
(see `docs/gpu-services`).
