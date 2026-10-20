# K0B0L Windows service orchestrator — the native stand-in for the POSIX
# swap supervisor (tools/gpu_swap_server.py). One 8 GB card, one resident
# service: this script is the stop/start dance a supervised Linux box does
# automatically, with the same music-run interlock rule the supervisor
# enforces (a live music generation is never killed for a text turn).
#
# Usage:
#   .\scripts\start-all.ps1              # router + webui (detach)
#   .\scripts\start-all.ps1 -Stop        # stop everything (except a live music run unless -Force)
#   .\scripts\start-all.ps1 -FreeGpu     # stop the router only (before a music/image run)
#   .\scripts\start-all.ps1 -Stop -Force # stop everything including a live music run
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$FreeGpu,
    [switch]$Force
)
$ErrorActionPreference = 'SilentlyContinue'

function Stop-ByPidFile([string]$PidFile, [string]$Name, [bool]$ForceIt) {
    # The music runner registers its engine PID at spawn (mirrors the
    # POSIX /tmp/k0b0l-music-engine.pid contract). A live engine is a
    # 30-60 min generation: refuse unless -Force.
    if (Test-Path $PidFile) {
        $p = Get-Content $PidFile -ErrorAction SilentlyContinue
        $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
        if ($proc) {
            if (-not $ForceIt) {
                Write-Host " !! a $Name run is LIVE (pid $p) — refusing to kill it (use -Force)" -ForegroundColor Yellow
                return $true
            }
            Stop-Process -Id $p -Force
            Write-Host " killed $Name (pid $p)" -ForegroundColor Yellow
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
    }
    return $false
}

$musicPid = Join-Path $env:TEMP 'k0b0l-music-engine.pid'

if ($Stop) {
    $live = Stop-ByPidFile $musicPid 'music generation' $Force
    Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-Process audiocpp_cli -ErrorAction SilentlyContinue | ForEach-Object {
        if (-not $live -or $Force) { Stop-Process -Id $_.Id -Force }
    }
    Write-Host 'stopped (router and any orphan engines)' -ForegroundColor Green
    exit 0
}

if ($FreeGpu) {
    $live = Stop-ByPidFile $musicPid 'music generation' $Force
    if (-not $live) {
        Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force
        Write-Host 'router stopped — GPU free for a music/image run' -ForegroundColor Green
    }
    exit 0
}

# default: start router + webui
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $here 'start-router.ps1') -Detach
& (Join-Path $here 'start-webui.ps1')
Write-Host 'K0B0L up: router :11434, webui :8321' -ForegroundColor Green
Write-Host 'Before a music run from the webui: .\scripts\start-all.ps1 -FreeGpu' -ForegroundColor DarkGray
