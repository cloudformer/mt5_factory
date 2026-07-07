# MT5 Factory - restart worker services + self-check (does NOT update code)
# Usage: powershell -ExecutionPolicy Bypass -File .\restart.ps1
$ErrorActionPreference = "Stop"
trap { Write-Host "!! Restart FAILED: $_" -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $root

Write-Host "=== Restart services ===" -ForegroundColor Cyan
# Dedicated worker VM: make sure old python processes are gone.
# Redirect inside cmd, not PS: under EAP=Stop, PS 5.1 turns taskkill's stderr
# ("process not found" - normal when nothing was running) into a fatal error.
cmd /c "taskkill /F /IM python.exe >nul 2>&1"
Start-Sleep -Seconds 2
# 必须经 explorer 启动: 若本脚本在管理员窗口运行, 直接 Start-Process 会把提升权限传给
# 子进程, 提升的 python 连不上普通权限的 MT5 终端; 经 explorer = 普通权限, 与双击一致
foreach ($bat in "start_bridge.bat", "start_runner.bat") {
    explorer.exe "$root\$bat"
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
