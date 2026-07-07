# MT5 Factory - update worker: pull latest code + deps, then restart + self-check
# Usage: powershell -ExecutionPolicy Bypass -File .\update.ps1
$ErrorActionPreference = "Stop"
trap { Write-Host "!! Update FAILED: $_" -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $root

Write-Host "=== Update code ===" -ForegroundColor Cyan
if (Test-Path "$repo\.git") {
    git -C $repo pull
} else {
    Write-Host "Not a git repo - skipped pull (copy files manually, then run this script)" -ForegroundColor Yellow
}
python -m pip install -r "$root\requirements.txt" --quiet

& "$root\restart.ps1"
