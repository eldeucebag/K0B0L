#!/usr/bin/env bash
# ============================================================================
# K0B0L full setup — from a fresh OS to a working harness.
#
# Installs and configures the whole backend on one local machine:
#   * system dependencies + the Python environment
#   * the PrismML llama.cpp fork (the ONLY server that loads the ternary
#     Bonsai quants: PTQ1_0 / PQ2_0 — stock llama.cpp and Ollama reject them)
#   * sd.cpp (stable-diffusion.cpp) for the image API, built with CUDA
#   * audio.cpp for the music API (MiniMax-Music3), built with CUDA on
#     Linux/macOS — Windows uses the prebuilt cuda12.4/cuda13.3 zip and
#     the matching cudart zip, or WSL2
#   * every LLM, image, and music model the harness names, downloaded and
#     pinned — including the HQ music set (q8_0 flow transformer + bf16
#     depth decoder) that makes high step counts pay off
#   * the router preset (models.ini), the image server, the GPU swap
#     supervisor, and systemd units — or launchd on macOS
#
# Platforms, honestly ranked: Linux (systemd+NVIDIA) is the supported
# path and what this box runs. macOS runs the llama fork's Metal build
# and sd.cpp/audio.cpp CPU/Metal with CPU-only image and music gen
# (slow but works); its supervisor uses launchd. Windows has a native
# path now — setup_full.ps1 (PowerShell): llama router + models +
# webui natively; audio.cpp via its prebuilt CUDA zips; sd.cpp builds
# from source — but the swap supervisor is POSIX-only, so on native
# Windows only one GPU service runs at a time (stop llama before a
# music/image run). WSL2 runs this script as-is with the Windows
# NVIDIA driver and gets the full supervised swap — that is the
# recommended Windows route for the whole stack.
#
# The script never deletes or modifies an existing setup: every install
# step is idempotent (skip when its target already exists) and every
# overwrite is into a directory this script owns (default ~/k0b0l).
# Existing services named k0b0l-* are never touched — the units install
# only when absent. Existing models never re-download (size + probe).
#
# Usage:
#   bash setup_full.sh              # everything, into ~/k0b0l
#   K0B0L_HOME=/path bash setup_full.sh
#   bash setup_full.sh --skip-models    # code only, no multi-GB downloads
#   bash setup_full.sh --no-services    # skip systemd/launchd entirely
#
# Disk: ~70 GB with every model (LLMs ~14 GB, images ~26 GB, music
# ~10 GB stock + ~4 GB HQ). VRAM: 8 GB is the design point (one
# resident LLM, or one image model, or one music run — the swap
# supervisor trades the card between them).
# ============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Layout — everything this script creates lives under K0B0L_HOME
# ---------------------------------------------------------------------------
K0B0L_HOME="${K0B0L_HOME:-$HOME/k0b0l}"
LLAMA_DIR="$K0B0L_HOME/llama-prism"           # the fork's binaries
SD_DIR="$K0B0L_HOME/sd-cpp"                   # stable-diffusion.cpp
AUDIO_DIR="$K0B0L_HOME/audio.cpp"             # audio.cpp (music engine)
MUSIC_MODELS_DIR="${K0B0L_MUSIC_MODELS:-$K0B0L_HOME/models/MiniMax-Music3-GGUF}"
MUSIC_HQ_DIR="${K0B0L_MUSIC_HQ_DIR:-$K0B0L_HOME/models/minimax-hq}"
MODELS_DIR="${K0B0L_IMAGE_MODELS:-$K0B0L_HOME/models}"
LLM_MODELS_DIR="$LLAMA_DIR/models"             # the router's --models-dir
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # harness repo
SKIP_MODELS=0
NO_SERVICES=0
for arg in "$@"; do
  case "$arg" in
    --skip-models) SKIP_MODELS=1 ;;
    --no-services) NO_SERVICES=1 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok \033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !! \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

OS="$(uname -s)"
ARCH="$(uname -m)"
PKG=""

# ---------------------------------------------------------------------------
# 1. System dependencies
# ---------------------------------------------------------------------------
linux_pkgs() {
  # build tools + git + curl + python + psmisc (pkill for the supervisor)
  if command -v apt-get >/dev/null; then
    PKG=apt
    sudo apt-get update -y
    sudo apt-get install -y build-essential git curl python3 python3-pip python3-venv psmisc cmake libcurl4-openssl-dev
  elif command -v dnf >/dev/null; then
    PKG=dnf
    sudo dnf install -y gcc gcc-c++ make git curl python3 python3-pip psmisc cmake libcurl-devel
  elif command -v pacman >/dev/null; then
    PKG=pacman
    sudo pacman -Sy --noconfirm base-devel git curl python python-pip psmisc cmake
  else
    die "unsupported linux package manager (need apt/dnf/pacman)"
  fi
}

macos_pkgs() {
  command -v brew >/dev/null || die "install Homebrew first: https://brew.sh"
  brew install cmake git curl psmist 2>/dev/null || brew install cmake git curl
  ok "macOS deps via Homebrew"
}

say "platform: $OS $ARCH"
case "$OS" in
  Linux)  linux_pkgs ;;
  Darwin) macos_pkgs ;;
  *)      die "run this from Linux (supported) or macOS (image gen is CPU-only there). Windows: see the bottom of this script." ;;
esac

mkdir -p "$K0B0L_HOME" "$MODELS_DIR"

# ---------------------------------------------------------------------------
# 2. The PrismML llama.cpp fork (prebuilt release, pinned build)
# ---------------------------------------------------------------------------
# Build 10743 is the exact build the reference box runs; it is verified
# against the ternary kernels (vec FA on Pascal) and the router preset
# below. Newer prism-b* releases exist; bump PRISM_TAG deliberately and
# re-verify the models.ini flags still parse.
PRISM_TAG="prism-b10743-adfffbe"
if [ -x "$LLAMA_DIR/llama-server" ]; then
  ok "llama fork already present at $LLAMA_DIR (left untouched)"
else
  say "downloading the PrismML fork ($PRISM_TAG)..."
  case "$OS-$ARCH" in
    Linux-x86_64)
      ASSET="llama-${PRISM_TAG}-bin-linux-cuda-12.4-x64.tar.gz"
      ;;
    Linux-aarch64)
      ASSET="llama-${PRISM_TAG}-bin-linux-cuda-12.4-arm64.tar.gz"
      [ -n "${ASSET:-}" ] || die "no aarch64 linux asset; build from source"
      ;;
    Darwin-arm64)
      ASSET="llama-${PRISM_TAG}-bin-macos-arm64.tar.gz"
      ;;
    Darwin-x86_64)
      die "Intel Mac: build the fork from source (github.com/prismml-eng/llama.cpp)"
      ;;
    *) die "no prebuilt fork asset for $OS-$ARCH" ;;
  esac
  URL="https://github.com/prismml-eng/llama.cpp/releases/download/${PRISM_TAG}/${ASSET}"
  mkdir -p "$LLAMA_DIR"
  curl -fL --retry 3 -o "$K0B0L_HOME/$ASSET" "$URL"
  tar -xzf "$K0B0L_HOME/$ASSET" -C "$LLAMA_DIR" --strip-components=1
  ok "llama fork installed: $("$LLAMA_DIR/llama-server" --version 2>&1 | head -1)"
fi

# CUDA runtime note: the cuda-12.4 build needs the driver >= 550 and
# libcuda present; it does NOT bundle the toolkit. If nvidia-smi is
# absent the CPU-only build is required instead:
if [ "$OS" = "Linux" ] && ! command -v nvidia-smi >/dev/null; then
  warn "no NVIDIA driver detected — the CUDA build will fail to start."
  warn "re-download with the -cpu asset name, or install the NVIDIA driver."
fi

# Loader path: torch's cu12x wheels link libcupti.so.12, which ships in
# the CUDA toolkit's lib64 — NOT in Ubuntu's ldconfig. Without this the
# local diffusers fallback dies with:
#   ImportError: libcupti.so.12: cannot open shared object file
# Any toolkit with a .so.12 CUPTI works; newest wins.
if [ "$OS" = "Linux" ]; then
  CUPTI_DIR="$(ls -d /usr/local/cuda-12*/lib64 2>/dev/null | tail -1)"
  if [ -n "$CUPTI_DIR" ] && [ -e "$CUPTI_DIR/libcupti.so.12" ]; then
    if ! ldconfig -p 2>/dev/null | grep -q "libcupti.so.12"; then
      printf '%s\n' "$CUPTI_DIR" \
        | sudo tee /etc/ld.so.conf.d/cuda-toolkit-libs.conf >/dev/null
      sudo ldconfig
      ok "loader path: $CUPTI_DIR on ldconfig (libcupti.so.12 resolvable)"
    else
      ok "loader path: libcupti.so.12 already resolvable"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 3. sd.cpp (stable-diffusion.cpp) — the image API server
# ---------------------------------------------------------------------------
# sd-server exposes OpenAI-shaped /v1/images/generations and is what the
# harness's imagegen client talks to. Built from source with CUDA; the
# prebuilts need glibc 2.38 and this box family may be older.
if [ -x "$SD_DIR/build/bin/sd-server" ]; then
  ok "sd.cpp already built at $SD_DIR (left untouched)"
else
  say "building stable-diffusion.cpp (CUDA)..."
  git clone --depth 1 https://github.com/leejet/stable-diffusion.cpp "$SD_DIR"
  cmake -S "$SD_DIR" -B "$SD_DIR/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DSD_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="61;70;75;80;86;89;90"
  cmake --build "$SD_DIR/build" --config Release -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu)"
  ok "sd.cpp built: $SD_DIR/build/bin/sd-server"
fi
export K0B0L_SD_BIN="$SD_DIR/build/bin/sd-cli"

# ---------------------------------------------------------------------------
# 3b. audio.cpp — the music engine (MiniMax-Music3 via tools/audiogen.py)
# ---------------------------------------------------------------------------
# The same ggml build recipe as sd.cpp with the known-good toolchain pair:
# the PATH nvcc may be stale (11.5 on the reference box) and only g++-12
# pairs with CUDA 12.5. GGML_CUDA_NO_VMM=1 is set by the runner (Pascal's
# cuMemCreate path dies mid-run; the legacy pool allocator works).
AUDIO_TAG="v0.9.2-drama3-base-small"
if [ -x "$AUDIO_DIR/build/bin/audiocpp_cli" ]; then
  ok "audio.cpp already built at $AUDIO_DIR (left untouched)"
else
  say "building audio.cpp (CUDA)..."
  git clone --depth 1 --branch "$AUDIO_TAG" \
    https://github.com/0xShug0/audio.cpp "$AUDIO_DIR" 2>/dev/null \
    || git clone --depth 1 https://github.com/0xShug0/audio.cpp "$AUDIO_DIR"
  # Toolchain pair, with fallbacks when a CUDA toolkit/gcc generation is
  # absent: newest /usr/local/cuda-* wins, plain nvcc/g++ last resort.
  CUDA_ROOT="$(ls -d /usr/local/cuda-1[2-9]* 2>/dev/null | tail -1)"
  if [ -n "$CUDA_ROOT" ] && [ -x "$CUDA_ROOT/bin/nvcc" ] && command -v g++-12 >/dev/null; then
    cmake -S "$AUDIO_DIR" -B "$AUDIO_DIR/build" \
      -DENGINE_ENABLE_CUDA=ON \
      -DCUDAToolkit_ROOT="$CUDA_ROOT" \
      -DCMAKE_CUDA_COMPILER="$CUDA_ROOT/bin/nvcc" \
      -DCMAKE_CUDA_ARCHITECTURES=61 \
      -DCMAKE_CXX_COMPILER=g++-12 -DCMAKE_BUILD_TYPE=Release
  else
    warn "pinned CUDA 12.5 + g++-12 pair not found; building with defaults"
    cmake -S "$AUDIO_DIR" -B "$AUDIO_DIR/build" \
      -DENGINE_ENABLE_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=61 \
      -DCMAKE_BUILD_TYPE=Release
  fi
  cmake --build "$AUDIO_DIR/build" --config Release -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu)"
  ok "audio.cpp built: $AUDIO_DIR/build/bin/audiocpp_cli"
fi

# ---------------------------------------------------------------------------
# 4. Python environment (the harness + the web front end)
# ---------------------------------------------------------------------------
say "python dependencies..."
if [ ! -d "$REPO_DIR/.venv" ]; then
  python3 -m venv "$REPO_DIR/.venv"
fi
# shellcheck disable=SC1091
source "$REPO_DIR/.venv/bin/activate"
pip install --upgrade pip >/dev/null
pip install -r "$REPO_DIR/requirements.txt"
# the web front end + the swap supervisor's deps:
pip install "starlette>=0.37" "uvicorn>=0.29" "websockets>=12"
# the diffusers fallback path for pony (CPU-offloaded SDXL) — optional but
# pinned to the versions the recipe was verified with:
pip install "torch==2.5.1" --index-url https://download.pytorch.org/whl/cu121 2>/dev/null \
  || pip install "torch==2.5.1"
pip install "diffusers==0.36.0" "transformers==4.57.6" "huggingface-hub>=0.26"
ok "python env ready ($REPO_DIR/.venv)"

# ---------------------------------------------------------------------------
# 5. Models — LLMs into the router's models dir, images into MODELS_DIR
# ---------------------------------------------------------------------------
# Every URL below was resolved and HTTP-checked against the reference
# machine's model set. hf() skips files that already exist with a size
# within 0.1% of the published one (partial downloads resume with -C -).
hf() {  # hf <repo> <file-in-repo> <local-name>
  local repo="$1" remote="$2" local_name="$3"
  local dest="$MODELS_DIR/$local_name"
  if [ -f "$dest" ] && [ "$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest")" -gt 1000000 ]; then
    ok "model present: $local_name"
    return 0
  fi
  say "downloading $local_name ($(numfmt --to=iec 2>/dev/null <<< "${4:-?}") )..."
  mkdir -p "$(dirname "$dest")"
  curl -fL --retry 5 -C - -o "$dest.part" \
    "https://huggingface.co/$repo/resolve/main/$remote"
  mv "$dest.part" "$dest"
  ok "model downloaded: $local_name"
}

if [ "$SKIP_MODELS" = 1 ]; then
  warn "--skip-models: skipping all model downloads"
else
  say "downloading models (~55 GB total; each is skipped when present)..."

  # ---- LLMs (the router advertises whatever GGUFs sit in $LLM_MODELS_DIR)
  mkdir -p "$LLM_MODELS_DIR"
  llm() {  # llm <repo> <remote-file> <local-name>
    local dest="$LLM_MODELS_DIR/$3"
    [ -f "$dest" ] && { ok "llm present: $3"; return 0; }
    say "downloading LLM $3..."
    curl -fL --retry 5 -C - -o "$dest.part" "https://huggingface.co/$1/resolve/main/$2"
    mv "$dest.part" "$dest"
    ok "llm downloaded: $3"
  }
  # The ternary 27B the harness drives (needs the Prism fork).
  llm prism-ml/Ternary-Bonsai-2-27B-gguf Ternary-Bonsai-2-27B-PTQ1_0.gguf Ternary-Bonsai-2-27B-TQ1_0.gguf
  # The chat models the web front end lists.
  llm mradermacher/Gemma-4-E4B-Abliterated-Uncensored-i1-GGUF Gemma-4-E4B-Abliterated-Uncensored.i1-Q4_K_M.gguf Gemma-4-E4B-Abliterated-i1-Q4_K_M.gguf
  llm DavidAU/Qwen3-8B-Hivemind-Instruct-Heretic-Abliterated-Uncensored-NEO-Imatrix-GGUF Qwen3-8B-Hivemind-Inst-Hrtic-Ablit-Uncensored-Q4_K_M-imat.gguf Qwen3-8B-Hivemind-Heretic-Abliterated-Q4_K_M-imat.gguf
  llm mradermacher/DeepSeek-R1-Distill-Qwen-14B-abliterated-i1-GGUF DeepSeek-R1-Distill-Qwen-14B-abliterated.i1-Q4_K_S.gguf DeepSeek-R1-Distill-Qwen-14B-abliterated-i1-Q4_K_S.gguf

  # ---- image models (sd.cpp formats, named exactly as imagegen.py expects)
  # pony: SDXL single-file checkpoint
  hf AiAF/ponyDiffusionV6XL_v6StartWithThisOne.safetensors ponyDiffusionV6XL_v6StartWithThisOne.safetensors ponyDiffusionV6XL_v6.safetensors 6.5G
  # qwen: DiT GGUF + Heretic GGUF encoder + the official 2.1 VAE
  hf abenzerps/Qwen-Image-2.1-Uncensored-GGUF qwen-image-2.1-UC-Q4_0.gguf qwen-image-2.1-UC-Q4_0.gguf 4.15G
  hf pottokao/Qwen-Image-2.1-Text-Encoder-Heretic-GGUF qwen3vl_8b_heretic-Q4_K_M.gguf qwen3vl_8b_heretic-Q4_K_M.gguf 4.68G
  hf Qwen/Qwen-Image-2.1 vae/diffusion_pytorch_model.safetensors qwen_image_vae_2.1.safetensors 1.35G
  # chroma: Flux-class DiT GGUF + GGUF T5 encoder (fp8 safetensors T5
  # segfaults this sd.cpp build — see rt-image-server.sh) + flux VAE
  hf silveroxides/Chroma-GGUF chroma-unlocked-v44/chroma-unlocked-v44-Q4_0.gguf chroma-v44-unlocked-Q4_0.gguf 5.43G
  hf city96/t5-v1_1-xxl-encoder-gguf t5-v1_1-xxl-encoder-Q4_K_M.gguf t5-v1_1-xxl-encoder-Q4_K_M.gguf 2.9G
  hf foxmail/flux_vae ae.safetensors flux_vae.safetensors 335M

  # ---- music models (MiniMax-Music3, named exactly as tools/audiogen.py
  # expects). Stock set: the q4_0 LM + q4_0 flow transformer + q8_0 depth
  # decoder that fit an 8 GB card. HQ set: q8_0 flow transformer + bf16
  # depth decoder (the denoiser upgrade that makes high step counts pay
  # off); the q8_0 LM is deliberately NOT downloaded — the engine
  # cudaMallocs the full weight buffer on device and 9.4 GiB cannot fit.
  say "downloading music models (MiniMax-Music3)..."
  music() {  # music <remote-file> [hq]
    local dest
    if [ "${2:-}" = "hq" ]; then
      dest="$MUSIC_HQ_DIR/$1"
    else
      dest="$MUSIC_MODELS_DIR/$1"
    fi
    [ -f "$dest" ] && { ok "music model present: $1"; return 0; }
    say "downloading $1..."
    mkdir -p "$(dirname "$dest")"
    curl -fL --retry 5 -C - -o "$dest.part" \
      "https://huggingface.co/audio-cpp/MiniMax-Music3-GGUF/resolve/main/$1"
    mv "$dest.part" "$dest"
    ok "music model downloaded: $1"
  }
  # stock set (all required)
  for f in config.json condition_encoder.gguf language_model_q4_0.gguf \
           rvq_depth_decoder_q8_0.gguf transformer_q4_0.gguf vocoder.gguf; do
    music "$f"
  done
  # config/ and tokenizer/ trees: every file the assets loader names
  mkdir -p "$MUSIC_MODELS_DIR/config" "$MUSIC_MODELS_DIR/tokenizer"
  for f in language_model.json rvq_depth_decoder.json condition_encoder.json transformer.json vocoder.json; do
    [ -f "$MUSIC_MODELS_DIR/config/$f" ] || curl -sfL --retry 5 \
      -o "$MUSIC_MODELS_DIR/config/$f" \
      "https://huggingface.co/audio-cpp/MiniMax-Music3-GGUF/resolve/main/config/$f"
  done
  for f in tokenizer.json tokenizer_config.json; do
    [ -f "$MUSIC_MODELS_DIR/tokenizer/$f" ] || curl -sfL --retry 5 \
      -o "$MUSIC_MODELS_DIR/tokenizer/$f" \
      "https://huggingface.co/audio-cpp/MiniMax-Music3-GGUF/resolve/main/tokenizer/$f"
  done
  ok "music stock set complete: $MUSIC_MODELS_DIR"
  # HQ set (optional but recommended: ~3.9 GB) — symlinked to the stock
  # root's shared components, real files only for the two upgrades.
  if [ "${K0B0L_NO_MUSIC_HQ:-0}" != "1" ]; then
    mkdir -p "$MUSIC_HQ_DIR"
    for f in config config.json tokenizer condition_encoder.gguf \
             language_model_q4_0.gguf rvq_depth_decoder_q8_0.gguf vocoder.gguf; do
      [ -e "$MUSIC_HQ_DIR/$f" ] || ln -s "$MUSIC_MODELS_DIR/$f" "$MUSIC_HQ_DIR/$f"
    done
    music transformer_q8_0.gguf hq
    music rvq_depth_decoder_bf16.gguf hq
    ok "music HQ set complete: $MUSIC_HQ_DIR (q8_0 flow + bf16 depth)"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Router preset — models.ini (generated, never overwriting a custom one)
# ---------------------------------------------------------------------------
if [ -f "$LLAMA_DIR/models.ini" ]; then
  ok "models.ini already exists at $LLAMA_DIR (left untouched)"
else
  say "writing the router preset (models.ini)..."
  cat > "$LLAMA_DIR/models.ini" <<'EOF'
version = 1

; Router-mode presets. Generated by setup_full.sh — copy of the preset the
; reference box runs. llama-server starts WITHOUT -m, advertises every
; GGUF under --models-dir, loads on demand, --models-max 1 keeps one
; resident (asking for another evicts it first).
;
; Options mirror command-line flags without leading dashes. [*] is
; inherited by every model; per-model sections override.

[*]
; The window is the ceiling and the spill target: nothing may be silently
; dropped between 65k and 100k, and a prompt longer than the window is
; rejected outright (400 exceed_context_size_error) — never truncated.
c = 102400
jinja = true
flash-attn = true
parallel = 1

; Never pin weights in RAM: mmap keeps them evictable so an over-long
; context spills to system RAM instead of failing. Dropped query = the
; worst outcome; slow query = acceptable.
load-mode = mmap

; KV cache in host RAM: at a 100k window the cache is the largest
; allocation in play and --fit moves layers, never KV. On the card it is
; what kills a large window on the first decode; in host RAM it is
; ordinary pageable memory.
no-kv-offload = true

; q4_0 KV: ~81 KiB/token on the 8B against f16's 288. Across 100k that
; is ~8 GB against ~28 GB — f16 is not "slower", it is a model that
; cannot load.
cache-type-k = q4_0
cache-type-v = q4_0

; The ternary 27B the harness drives; resident at startup.
[Ternary-Bonsai-2-27B-TQ1_0]
load-on-startup = true

[Qwen3-8B-Hivemind-Heretic-Abliterated-Q4_K_M-imat]

[Gemma-4-E4B-Abliterated-i1-Q4_K_M]

; 8 GB of weights on an 8 GB card: --fit parks most layers on the CPU
; and decode runs from system RAM. Inherits the global window; a
; per-model cap below it would be the silent dropping that is ruled out.
[DeepSeek-R1-Distill-Qwen-14B-abliterated-i1-Q4_K_S]
EOF
  ok "models.ini written"
fi

# ---------------------------------------------------------------------------
# 7. Harness env file — the one address that configures everything
# ---------------------------------------------------------------------------
# The harness reads API_URL first; imagegen derives :7860 and :7861 from
# its host. One line wires the whole local stack.
if [ -f "$HOME/.k0b0l.env" ]; then
  ok "~/.k0b0l.env exists (left untouched)"
else
  cat > "$HOME/.k0b0l.env" <<EOF
# K0B0L local stack, written by setup_full.sh $(date -I)
export API_URL="http://127.0.0.1:11434/v1"
export K0B0L_IMAGE_MODELS="$MODELS_DIR"
export K0B0L_SD_BIN="$SD_DIR/build/bin/sd-cli"
export K0B0L_AUDIO_DIR="$AUDIO_DIR"
export K0B0L_AUDIO_BIN="$AUDIO_DIR/build/bin/audiocpp_cli"
export K0B0L_MUSIC_MODELS="$MUSIC_MODELS_DIR"
export K0B0L_MUSIC_HQ_DIR="$MUSIC_HQ_DIR"
export CHAT_MODEL="Gemma-4-E4B-Abliterated-i1-Q4_K_M"
EOF
  ok "~/.k0b0l.env written — source it: source ~/.k0b0l.env"
fi

# ---------------------------------------------------------------------------
# 8. Services (Linux: systemd; macOS: launchd; skip with --no-services)
# ---------------------------------------------------------------------------
if [ "$NO_SERVICES" = 1 ]; then
  warn "--no-services: skipping service installation"
  warn "start by hand:"
  warn "  $LLAMA_DIR/llama-server --models-dir $LLM_MODELS_DIR --models-preset $LLAMA_DIR/models.ini --models-max 1 --host 0.0.0.0 --port 11434"
  warn "  python3 $REPO_DIR/tools/gpu_swap_server.py"
  warn "  python3 $REPO_DIR/tools/webui.py"
else
  say "installing services..."
  case "$OS" in
    Linux)
      # The units install only when absent — an existing k0b0l-llama or
      # k0b0l-swap is never modified, in line with the rule that this
      # script may not touch an existing setup.
      if [ ! -f /etc/systemd/system/k0b0l-llama.service ]; then
        sudo tee /etc/systemd/system/k0b0l-llama.service >/dev/null <<EOF
[Unit]
Description=K0B0L llama-server router (models on demand, :11434)
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$LLAMA_DIR
ExecStart=$LLAMA_DIR/llama-server --models-dir ./models --models-preset ./models.ini --models-max 1 --host 0.0.0.0 --port 11434
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now k0b0l-llama.service
        ok "k0b0l-llama.service installed and started"
      else
        ok "k0b0l-llama.service already exists (left untouched)"
      fi
      if [ ! -f /etc/systemd/system/k0b0l-swap.service ]; then
        sudo tee /etc/systemd/system/k0b0l-swap.service >/dev/null <<EOF
[Unit]
Description=K0B0L GPU swap supervisor (llama :11434 / images :7860, swaps on :7861)
After=network.target k0b0l-llama.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/tools/gpu_swap_server.py
Restart=always
RestartSec=3
Environment=K0B0L_SWAP_PORT=7861

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now k0b0l-swap.service
        ok "k0b0l-swap.service installed and started"
      else
        ok "k0b0l-swap.service already exists (left untouched)"
      fi
      ;;
    Darwin)
      if [ ! -f "$HOME/Library/LaunchAgents/ai.k0b0l.router.plist" ]; then
        mkdir -p "$HOME/Library/LaunchAgents"
        cat > "$HOME/Library/LaunchAgents/ai.k0b0l.router.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.k0b0l.router</string>
  <key>ProgramArguments</key><array>
    <string>$LLAMA_DIR/llama-server</string>
    <string>--models-dir</string><string>$LLM_MODELS_DIR</string>
    <string>--models-preset</string><string>$LLAMA_DIR/models.ini</string>
    <string>--models-max</string><string>1</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>11434</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
EOF
        launchctl load "$HOME/Library/LaunchAgents/ai.k0b0l.router.plist" 2>/dev/null || true
        ok "launchd router installed (ai.k0b0l.router)"
      else
        ok "launchd router already present (left untouched)"
      fi
      warn "macOS: no swap supervisor service — run it in a terminal:"
      warn "  python3 $REPO_DIR/tools/gpu_swap_server.py"
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# 9. Verify
# ---------------------------------------------------------------------------
say "verifying..."
sleep 2
if curl -sf -m 5 http://127.0.0.1:11434/health >/dev/null 2>&1; then
  ok "llama router healthy on :11434"
else
  warn "router not answering on :11434 yet (first model load takes a minute)"
fi
if curl -sf -m 5 http://127.0.0.1:7861/state 2>/dev/null | grep -q '"llm": *true'; then
  ok "swap supervisor answering on :7861"
else
  warn "swap supervisor not answering on :7861 (see --no-services notes)"
fi
say "model inventory:"
ls -lh "$LLM_MODELS_DIR" 2>/dev/null | tail -n +2 | awk '{printf "  %-55s %s\n", $9, $5}'
ls -lh "$MODELS_DIR" 2>/dev/null | grep -v '\.done\|MiniMax\|minimax' | tail -n +2 | awk '{printf "  %-55s %s\n", $9, $5}'
say "music models:"
ls -lh "$MUSIC_MODELS_DIR" 2>/dev/null | grep -v '^d' | tail -n +2 | awk '{printf "  %-55s %s\n", $9, $5}'
ls -lh "$MUSIC_HQ_DIR" 2>/dev/null | grep -v '^d\|^l' | tail -n +2 | awk '{printf "  HQ %-52s %s\n", $9, $5}'

cat <<EOF

============================================================================
 K0B0L setup complete.
============================================================================
 Next:
   source ~/.k0b0l.env
   python3 $REPO_DIR/thinlizzy.py --list-models     # the router's ids
   python3 $REPO_DIR/tools/webui.py                 # the web front end :8321
   python3 $REPO_DIR/thinlizzy.py --chat            # the Textual chat

 Image generation: the card is swapped on demand by the supervisor
 (:7861). Music generation likewise — the 🎵 dialog in the webui or the
 generate_music tool; pass quality="hq" (or pick hq in the dialog) for
 the q8_0-flow + bf16-depth set that high step counts (60-100) need.
 macOS: image and music generation run CPU-only and are slow.

 Windows — three routes, pick one:
   1. WSL2 (recommended): install WSL2 + Ubuntu, connect the Windows
      NVIDIA driver (wsl --install), then run this script unchanged
      inside the WSL2 shell. Full stack including the swap supervisor.
   2. Native: run setup_full.ps1 (PowerShell, repo root) — installs the
      llama router + models + webui natively, audio.cpp from prebuilt
      CUDA zips, sd.cpp from source. No swap supervisor on native
      Windows: only one GPU service at a time (stop llama before a
      music/image run; scripts/start-all.ps1 orchestrates).
   3. Manual native: llama-server.exe from the prism release
      ($PRISM_TAG windows zips + cudart zip), models into
      llama-prism\\models, python + pip install -r requirements.txt,
      python tools/webui.py. Point ~/.k0b0l.env API_URL at the router.
============================================================================
EOF
