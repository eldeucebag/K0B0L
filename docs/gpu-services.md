# GPU-box service layout (systemd)

This box serves **both** inference surfaces to the network — text on
:11434, images on :7860 — from one 8 GB card. Two systemd units own the
processes so everything survives a reboot:

```bash
sudo systemctl status k0b0l-swap    # the supervisor (:7861)
sudo systemctl status k0b0l-llama    # the llama router (:11434)
```

| unit | port | role |
| --- | --- | --- |
| `k0b0l-swap` | 7861 | the swap supervisor (`tools/gpu_swap_server.py`); owns both services, swaps the card on request |
| `k0b0l-llama` | 11434 | llama-server router, models loaded on demand (`models.ini`, `--models-max 1`) |
| *(on demand)* | 7860 | `sd-server` (sd.cpp), one image model resident, started by the supervisor per swap |

The image server is **not** a unit: it exists only while the card is
swapped to images, and the supervisor starts/stops it. That is the
point of the supervisor — only one service can hold the card.

## The swap API (from anywhere on the LAN)

```bash
curl http://GPUBOX:7861/state
# {"llm": true, "image": false, "image_models": ["pony", "qwen", "chroma"]}

curl -X POST http://GPUBOX:7861/swap/image -d '{"model": "pony"}'
# stops llama, loads the image model; ~10-30 s (weights on NTFS)

curl -X POST http://GPUBOX:7861/swap/llm -d '{}'
# stops sd-server, restarts the router
```

## The image API (OpenAI-shaped, on :7860 while swapped)

```bash
curl http://GPUBOX:7860/v1/models
curl -X POST http://GPUBOX:7860/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "a lighthouse at dusk", "n": 1, "size": "768x768"}'
# -> {"data": [{"b64_json": "..."}]}
```

Native knobs (steps, seed, negative prompt, sampler) ride inside the
prompt via sd.cpp's extension:
`text <sd_cpp_extra_args>{"sample_params":{"sample_steps":20}}</sd_cpp_extra_args>`.

## Who uses what

- The harness's `generate_image` tool and `/image` are **API-first**:
  they generate over `:7860`, and when the image service is down but
  `:7861` answers, they swap → generate → swap back automatically.
- A harness on the *other* machine sets `K0B0L_IMAGE_API` and
  `K0B0L_SWAP_API` to this box's LAN address — same behaviour, no
  local fallback.
- Remote clients (curl, scripts) can drive the swap directly.

## Unit files

The installed units live in `/etc/systemd/system/`:
`k0b0l-swap.service`, `k0b0l-llama.service`. Their sources are kept in
this repo under `ops/` — reinstall with:

```bash
sudo cp ops/*.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now k0b0l-swap k0b0l-llama
```
