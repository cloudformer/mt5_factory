# MT5 Factory - restart worker services + self-check (does NOT update code)
# Usage: powershell -ExecutionPolicy Bypass -File .\restart.ps1
$ErrorActionPreference = "Stop"
trap { Write-Host "!! Restart FAILED: $_" -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $root

Write-Host "=== Restart services ===" -ForegroundColor Cyan
if (-not (Get-ScheduledTask -TaskName "MT5Bridge" -ErrorAction SilentlyContinue)) {
    Write-Host "!! Scheduled tasks not found - run setup.bat first" -ForegroundColor Red
    exit 1
}
foreach ($t in "MT5Bridge", "MT5Runner") {
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
}
# Dedicated worker VM: make sure old python processes are gone
taskkill /F /IM python.exe 2>$null | Out-Null
Start-Sleep -Seconds 2
foreach ($t in "MT5Bridge", "MT5Runner") {
    Start-ScheduledTask -TaskName $t
}

Write-Host "=== Self-check ===" -ForegroundColor Cyan
$port = 8020
$m = Select-String -Path "$repo\env\.dev.env" -Pattern '^MT5_PORT=(\d+)' -ErrorAction SilentlyContinue
if ($m) { $port = [int]$m.Matches.Groups[1].Value }
$health = $null
foreach ($i in 1..12) {
    Start-Sleep -Seconds 5
    try { $health = Invoke-RestMethod "http://localhost:$port/health" -TimeoutSec 3; break } catch { }
}
if ($null -eq $health) {
    Write-Host "!! bridge did not respond within 60s - run start_bridge.bat manually to see the error" -ForegroundColor Red
    exit 1
}
Write-Host "bridge: $($health.status)" -ForegroundColor $(if ($health.status -eq 'healthy') { 'Green' } else { 'Yellow' })
Write-Host "Restart done" -ForegroundColor Green
