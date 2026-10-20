# Start the K0B0L web UI (http://LAN-IP:8321) in a new window.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$py = "$RepoDir\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = 'python' }
Start-Process -FilePath $py -ArgumentList "$RepoDir\tools\webui.py" -WorkingDirectory $RepoDir
Start-Sleep -Seconds 2
Write-Host 'webui starting on :8321' -ForegroundColor Green
