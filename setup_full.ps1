# ============================================================================
# K0B0L Windows setup — from a fresh Windows 10/11 box to a working harness.
# Native PowerShell port of setup_full.sh. What it installs:
#   * the PrismML llama.cpp fork (Windows CUDA build + cudart runtime)
#   * every LLM the harness names, downloaded and pinned
#   * audio.cpp (the music engine) from the prebuilt CUDA zips
#   * the Python environment + the web front end
#   * a models.ini router preset and a .k0b0l-env.ps1 environment file
#
# Honest scope (see setup_full.sh for the full-platform picture):
#   * No swap supervisor on native Windows — tools/gpu_swap_server.py is
#     POSIX-only (systemctl/pkill//var/tmp). Only ONE GPU service runs at
#     a time: stop the router before a music/image run. start-all.ps1
#     orchestrates the manual swap.
#   * sd.cpp (image generation) is NOT installed by default: its Windows
#     CUDA build needs Visual Studio + the CUDA toolkit. Run with
#     -WithImages to attempt the source build, or use WSL2 where
#     setup_full.sh does everything, supervised.
#   * Recommended Windows route for the FULL stack (images + music +
#     supervised GPU swap): WSL2. This script is for a native install.
#
# The script is idempotent: every step skips when its target exists, and
# existing services/configs are never touched.
#
# Usage (PowerShell, as your user — no admin needed except the VC redist):
#   .\setup_full.ps1                    # everything, into ~\k0b0l
#   .\setup_full.ps1 -K0B0LHome D:\k0b0l
#   .\setup_full.ps1 -SkipModels        # code only, no multi-GB downloads
#   .\setup_full.ps1 -WithImages        # also build sd.cpp (needs VS + CUDA)
# ============================================================================
[CmdletBinding()]
param(
    [string]$K0B0LHome = "$HOME\k0b0l",
    [switch]$SkipModels,
    [switch]$WithImages
)

$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- tiny console helpers ---------------------------------------------------
function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host " ok $m" -ForegroundColor Green }
function Warn($m) { Write-Host " !! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "FAIL $m" -ForegroundColor Red; exit 1 }

# --- layout ------------------------------------------------------------------
$LLamaDir      = Join-Path $K0B0LHome 'llama-prism'
$AudioDir      = Join-Path $K0B0LHome 'audio.cpp'
$SdDir         = Join-Path $K0B0LHome 'sd-cpp'
$ModelsDir     = Join-Path $K0B0LHome 'models'
$LlmModelsDir  = Join-Path $LLamaDir 'models'
$MusicModelsDir = Join-Path $ModelsDir 'MiniMax-Music3-GGUF'
$MusicHqDir    = Join-Path $ModelsDir 'minimax-hq'

Say "platform: Windows $( [Environment]::OSVersion.Version )"
if ($PSVersionTable.PSVersion.Major -lt 5) { Die 'PowerShell 5+ required' }

New-Item -ItemType Directory -Force -Path $K0B0LHome, $ModelsDir | Out-Null

# --- 1. GPU sanity ------------------------------------------------------------
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
    Ok "GPU: $gpu"
} else {
    Warn 'nvidia-smi not found — the CUDA builds will not run. Install the NVIDIA driver.'
}

# --- 2. Python (the harness + webui) ------------------------------------------
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Die 'python not found. Install Python 3.10+ from python.org (check "Add to PATH").'
}
Say 'python dependencies...'
if (-not (Test-Path "$RepoDir\.venv")) { & python -m venv "$RepoDir\.venv" }
& "$RepoDir\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$RepoDir\.venv\Scripts\python.exe" -m pip install -r "$RepoDir\requirements.txt" --quiet
# web front end deps (same pins as setup_full.sh)
& "$RepoDir\.venv\Scripts\python.exe" -m pip install "starlette>=0.37" "uvicorn>=0.29" "websockets>=12" --quiet
Ok "python env ready ($RepoDir\.venv)"

# --- 3. The PrismML llama.cpp fork (Windows CUDA prebuilt) --------------------
# Same pinned build as the Linux setup; verified against the ternary
# kernels and the router preset below.
$PrismTag = 'prism-b10743-adfffbe'
$llamaExe = Join-Path $LLamaDir 'llama-server.exe'
if (Test-Path $llamaExe) {
    Ok "llama fork already present at $LLamaDir (left untouched)"
} else {
    Say "downloading the PrismML fork ($PrismTag) Windows CUDA build..."
    New-Item -ItemType Directory -Force -Path $LLamaDir | Out-Null
    $zip = "$K0B0LHome\llama-win.zip"
    $url = "https://github.com/prismml-eng/llama.cpp/releases/download/$PrismTag/llama-$PrismTag-bin-win-cuda-12.4-x64.zip"
    & curl.exe -fL --retry 3 -o $zip $url
    # -DestinationPath needs the dir to exist; extract in place
    Expand-Archive -Path $zip -DestinationPath $LLamaDir -Force
    # the cudart runtime zip: the CUDA backend needs cudart/cuBLAS DLLs
    $cudart = "$K0B0LHome\llama-cudart.zip"
    $cudartUrl = "https://github.com/prismml-eng/llama.cpp/releases/download/$PrismTag/cudart-llama-bin-win-cuda-12.4-x64.zip"
    & curl.exe -fL --retry 3 -o $cudart $cudartUrl
    Expand-Archive -Path $cudart -DestinationPath $LLamaDir -Force
    Remove-Item $zip, $cudart -ErrorAction SilentlyContinue
    Ok "llama fork installed: $(& $llamaExe --version 2>&1 | Select-Object -First 1)"
}

# --- 4. audio.cpp (the music engine) — prebuilt Windows CUDA zips --------------
# audio.cpp publishes Windows x64 CUDA builds + a separate cudart zip.
# The runner (tools/audiogen.py) calls audiocpp_cli with --backend cuda.
$AudioTag   = 'v0.9.2-drama3-base-small'
$AudioExe   = Join-Path $AudioDir 'audiocpp_cli.exe'
if (Test-Path $AudioExe) {
    Ok "audio.cpp already present at $AudioDir (left untouched)"
} else {
    Say "downloading audio.cpp ($AudioTag) Windows CUDA build..."
    New-Item -ItemType Directory -Force -Path $AudioDir | Out-Null
    $zip = "$K0B0LHome\audio-win.zip"
    & curl.exe -fL --retry 3 -o $zip `
        "https://github.com/0xShug0/audio.cpp/releases/download/$AudioTag/audio-$AudioTag-bin-windows-x64-cuda12.4.zip"
    Expand-Archive -Path $zip -DestinationPath $AudioDir -Force
    $cudart = "$K0B0LHome\audio-cudart.zip"
    & curl.exe -fL --retry 3 -o $cudart `
        "https://github.com/0xShug0/audio.cpp/releases/download/$AudioTag/audio-$AudioTag-cudart-windows-x64-cuda12.4.zip"
    # cudart DLLs go NEXT TO the binaries so ggml-cuda.dll resolves them
    Expand-Archive -Path $cudart -DestinationPath $AudioDir -Force
    Remove-Item $zip, $cudart -ErrorAction SilentlyContinue
    if (Test-Path $AudioExe) {
        Ok "audio.cpp installed: $AudioExe"
    } else {
        Warn "audiocpp_cli.exe not at the expected path — check $AudioDir layout"
    }
}

# --- 5. sd.cpp (image generation) — opt-in source build ------------------------
if ($WithImages) {
    if (Test-Path (Join-Path $SdDir 'build\bin\sd-server.exe')) {
        Ok "sd.cpp already built at $SdDir (left untouched)"
    } else {
        if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) { Die 'cmake not found — install CMake + Visual Studio Build Tools + CUDA toolkit, or drop -WithImages and use WSL2.' }
        Say 'building stable-diffusion.cpp (CUDA)...'
        & git clone --depth 1 https://github.com/leejet/stable-diffusion.cpp $SdDir
        & cmake -S $SdDir -B "$SdDir\build" -DSD_CUDA=ON -DCMAKE_BUILD_TYPE=Release
        & cmake --build "$SdDir\build" --config Release
        Ok "sd.cpp built: $SdDir\build\bin\sd-server.exe"
    }
}

# --- 6. Models ------------------------------------------------------------------
if ($SkipModels) {
    Warn '-SkipModels: skipping all model downloads'
} else {
    Say 'downloading models (~40 GB LLM + music; each is skipped when present)...'
    function Get-HfModel([string]$Repo, [string]$Remote, [string]$Dest) {
        if (Test-Path $Dest) { Ok "model present: $(Split-Path -Leaf $Dest)"; return }
        Say "downloading $(Split-Path -Leaf $Dest)..."
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
        & curl.exe -fL --retry 5 -C - -o "$Dest.part" "https://huggingface.co/$Repo/resolve/main/$Remote"
        Move-Item "$Dest.part" $Dest -Force
        Ok "model downloaded: $(Split-Path -Leaf $Dest)"
    }
    # LLMs into the router's models dir
    New-Item -ItemType Directory -Force -Path $LlmModelsDir | Out-Null
    Get-HfModel 'prism-ml/Ternary-Bonsai-2-27B-gguf' 'Ternary-Bonsai-2-27B-PTQ1_0.gguf' (Join-Path $LlmModelsDir 'Ternary-Bonsai-2-27B-TQ1_0.gguf')
    Get-HfModel 'mradermacher/Gemma-4-E4B-Abliterated-Uncensored-i1-GGUF' 'Gemma-4-E4B-Abliterated-Uncensored.i1-Q4_K_M.gguf' (Join-Path $LlmModelsDir 'Gemma-4-E4B-Abliterated-i1-Q4_K_M.gguf')
    Get-HfModel 'DavidAU/Qwen3-8B-Hivemind-Instruct-Heretic-Abliterated-Uncensored-NEO-Imatrix-GGUF' 'Qwen3-8B-Hivemind-Inst-Hrtic-Ablit-Uncensored-Q4_K_M-imat.gguf' (Join-Path $LlmModelsDir 'Qwen3-8B-Hivemind-Heretic-Abliterated-Q4_K_M-imat.gguf')
    Get-HfModel 'mradermacher/DeepSeek-R1-Distill-Qwen-14B-abliterated-i1-GGUF' 'DeepSeek-R1-Distill-Qwen-14B-abliterated.i1-Q4_K_S.gguf' (Join-Path $LlmModelsDir 'DeepSeek-R1-Distill-Qwen-14B-abliterated-i1-Q4_K_S.gguf')

    # Music: stock MiniMax-Music3 set (fits an 8 GB card) + the HQ denoiser
    # set (q8_0 flow transformer + bf16 depth decoder). The q8_0 LM is NOT
    # downloaded: the engine cudaMallocs the full weight buffer on device
    # and 9.4 GiB cannot fit — same verdict as the Linux setup.
    Say 'downloading music models (MiniMax-Music3)...'
    foreach ($f in 'config.json','condition_encoder.gguf','language_model_q4_0.gguf',
                   'rvq_depth_decoder_q8_0.gguf','transformer_q4_0.gguf','vocoder.gguf') {
        Get-HfModel 'audio-cpp/MiniMax-Music3-GGUF' $f (Join-Path $MusicModelsDir $f)
    }
    New-Item -ItemType Directory -Force -Path "$MusicModelsDir\config", "$MusicModelsDir\tokenizer" | Out-Null
    foreach ($f in 'language_model.json','rvq_depth_decoder.json','condition_encoder.json','transformer.json','vocoder.json') {
        Get-HfModel 'audio-cpp/MiniMax-Music3-GGUF' "config/$f" (Join-Path $MusicModelsDir "config\$f")
    }
    foreach ($f in 'tokenizer.json','tokenizer_config.json') {
        Get-HfModel 'audio-cpp/MiniMax-Music3-GGUF' "tokenizer/$f" (Join-Path $MusicModelsDir "tokenizer\$f")
    }
    # HQ set: real files for the two upgrades, junctions for the shared rest
    New-Item -ItemType Directory -Force -Path $MusicHqDir | Out-Null
    foreach ($f in 'config','config.json','tokenizer','condition_encoder.gguf',
                   'language_model_q4_0.gguf','rvq_depth_decoder_q8_0.gguf','vocoder.gguf') {
        $link = Join-Path $MusicHqDir $f
        if (-not (Test-Path $link)) {
            New-Item -ItemType Junction -Path $link -Target (Join-Path $MusicModelsDir $f) | Out-Null
        }
    }
    Get-HfModel 'audio-cpp/MiniMax-Music3-GGUF' 'transformer_q8_0.gguf' (Join-Path $MusicHqDir 'transformer_q8_0.gguf')
    Get-HfModel 'audio-cpp/MiniMax-Music3-GGUF' 'rvq_depth_decoder_bf16.gguf' (Join-Path $MusicHqDir 'rvq_depth_decoder_bf16.gguf')
    Ok "music HQ set complete: $MusicHqDir (q8_0 flow + bf16 depth)"
}

# --- 7. Router preset — models.ini (generated, never overwriting a custom one) --
$iniPath = Join-Path $LLamaDir 'models.ini'
if (Test-Path $iniPath) {
    Ok "models.ini already exists at $LLamaDir (left untouched)"
} else {
    Say 'writing the router preset (models.ini)...'
    @'
version = 1

; Router-mode presets. Generated by setup_full.ps1 — copy of the preset
; the reference box runs. llama-server starts WITHOUT -m, advertises every
; GGUF under --models-dir, loads on demand, --models-max 1 keeps one
; resident (asking for another evicts it first).

[*]
c = 102400
jinja = true
flash-attn = true
parallel = 1
load-mode = mmap
no-kv-offload = true
cache-type-k = q4_0
cache-type-v = q4_0

[Ternary-Bonsai-2-27B-TQ1_0]
load-on-startup = true

[Qwen3-8B-Hivemind-Heretic-Abliterated-Q4_K_M-imat]

[Gemma-4-E4B-Abliterated-i1-Q4_K_M]

[DeepSeek-R1-Distill-Qwen-14B-abliterated-i1-Q4_K_S]
'@ | Set-Content -Path $iniPath -Encoding ascii
    Ok 'models.ini written'
}

# --- 8. Environment file ----------------------------------------------------------
$envPath = "$HOME\.k0b0l-env.ps1"
if (Test-Path $envPath) {
    Ok "$envPath exists (left untouched)"
} else {
    Say "writing $envPath ..."
    @"
# K0B0L local stack (Windows), written by setup_full.ps1
`$env:API_URL = 'http://127.0.0.1:11434/v1'
`$env:K0B0L_IMAGE_MODELS = '$ModelsDir'
`$env:K0B0L_AUDIO_DIR = '$AudioDir'
`$env:K0B0L_AUDIO_BIN = '$AudioExe'
`$env:K0B0L_MUSIC_MODELS = '$MusicModelsDir'
`$env:K0B0L_MUSIC_HQ_DIR = '$MusicHqDir'
`$env:CHAT_MODEL = 'Gemma-4-E4B-Abliterated-i1-Q4_K_M'
# native Windows has no swap supervisor; one GPU service at a time.
"@ | Set-Content -Path $envPath -Encoding ascii
    Ok "$envPath written — dot-source it: . $envPath"
}

# --- 9. Verify ----------------------------------------------------------------------
Say 'verifying...'
Start-Sleep -Seconds 2
if (Test-Path $llamaExe) { Ok "router binary present: $llamaExe" } else { Warn 'router binary missing' }
if (Test-Path $AudioExe) { Ok "music engine present: $AudioExe" } else { Warn 'music engine missing' }

Write-Host @"

============================================================================
 K0B0L Windows setup complete.
============================================================================
 Next:
   . $envPath
   Start the router:    scripts\start-router.ps1     (or llama-server.exe
        --models-dir models --models-preset models.ini --models-max 1
        --host 0.0.0.0 --port 11434  inside $LLamaDir)
   Start the webui:     scripts\start-webui.ps1
   Or both with a music-run interlock: scripts\start-all.ps1

 Music generation: the webui's music dialog or the generate_music tool.
 quality='hq' uses the q8_0 flow transformer + bf16 depth decoder that
 high step counts (60-100) need. STOP the router before a music run on
 native Windows — there is no swap supervisor; start-all.ps1 handles
 the stop/start dance for you.

 Image generation: run .\setup_full.ps1 -WithImages (needs VS Build
 Tools + CUDA toolkit), or use WSL2 where setup_full.sh does the whole
 supervised stack. WSL2 is the recommended full-stack Windows route.
============================================================================
"@ -ForegroundColor White
