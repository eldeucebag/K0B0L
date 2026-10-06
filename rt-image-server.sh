#!/usr/bin/env bash
# K0B0L image API server: sd.cpp's sd-server on :7860, bound to the LAN.
#
# Topology: THIS box is the GPU server. Text inference (llama-server :11434)
# and image generation (sd-server :7860) both run here; harness clients
# elsewhere reach both over HTTP. The two services share the one GPU:
# start the image server only while the llama-server router is stopped.
#
# Usage:
#   ./rt-image-server.sh pony|qwen|chroma   (default: pony)
set -u
MODEL="${1:-pony}"
DIR=/mnt/k0b0l-models
BIN=/tmp/sd-cpp/build/bin/sd-server

case "$MODEL" in
  pony)
    # The SDXL single-file checkpoint carries its own VAE; --model takes
    # full checkpoints, --diffusion-model takes standalone DiT weights.
    exec "$BIN" \
      --model "$DIR/ponyDiffusionV6XL_v6.safetensors" \
      -l 0.0.0.0 --listen-port 7860 --max-vram 6 --offload-to-cpu
    ;;
  qwen)
    exec "$BIN" \
      --diffusion-model "$DIR/qwen-image-2.1-UC-Q4_0.gguf" \
      --llm "$DIR/qwen3vl_8b_heretic-Q4_K_M.gguf" \
      --vae "$DIR/qwen_image_vae_2.1.safetensors" \
      -l 0.0.0.0 --listen-port 7860 \
      --backend clip=cpu,vae=cpu --max-vram 6 --offload-to-cpu
    ;;
  chroma)
    exec "$BIN" \
      --diffusion-model "$DIR/chroma-v44-unlocked-Q4_0.gguf" \
      --t5xxl "$DIR/t5_xxl_fp8_e4m3fn.safetensors" \
      --vae "$DIR/flux_vae.safetensors" \
      -l 0.0.0.0 --listen-port 7860 \
      --backend clip=cpu,vae=cpu --max-vram 6 --offload-to-cpu
    ;;
  *)
    echo "unknown model: $MODEL (pony|qwen|chroma)" >&2
    exit 2
    ;;
esac
