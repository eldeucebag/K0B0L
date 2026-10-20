# Start the K0B0L llama router (the only GPU service it should share with
# nothing: stop it before a music/image run on native Windows).
# Usage: .\scripts\start-router.ps1 [-Detach]
[CmdletBinding()]
param([switch]$Detach)
$ErrorActionPreference = 'Stop'
. "$HOME\.k0b0l-env.ps1" 2>$null
$LLamaDir = Join-Path "$HOME\k0b0l" 'llama-prism'
if (-not (Test-Path "$LLamaDir\llama-server.exe")) { Write-Host 'llama-server.exe not found — run setup_full.ps1 first' -ForegroundColor Red; exit 1 }
if (Get-Process llama-server -ErrorAction SilentlyContinue) { Write-Host 'router already running' -ForegroundColor Yellow; exit 0 }
$arg = @('--models-dir','models','--models-preset','models.ini','--models-max','1','--host','0.0.0.0','--port','11434')
if ($Detach) {
    Start-Process -FilePath "$LLamaDir\llama-server.exe" -ArgumentList $arg -WorkingDirectory $LLamaDir -WindowStyle Minimized
    Start-Sleep -Seconds 2
} else {
    Push-Location $LLamaDir
    & .\llama-server.exe @arg
    Pop-Location
}
