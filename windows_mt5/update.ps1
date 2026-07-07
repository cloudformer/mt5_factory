# MT5 Factory - update worker: pull latest code + deps, then restart + self-check
# Usage: powershell -ExecutionPolicy Bypass -File .\update.ps1
$ErrorActionPreference = "Stop"
trap { Write-Host "!! Update FAILED: $_" -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $root

Write-Host "=== Update code ===" -ForegroundColor Cyan
if ((Test-Path "$repo\.git") -and (Get-Command git -ErrorAction SilentlyContinue)) {
    git -C $repo pull
} else {
    Write-Host "No git repo or git not installed - skipped pull (copy files manually, then run this script)" -ForegroundColor Yellow
}
python -m pip install -r "$root\requirements.txt" --quiet

& "$root\restart.ps1"
