# MT5 Factory - restart worker services + self-check (does NOT update code)
# Usage: powershell -ExecutionPolicy Bypass -File .\restart.ps1
$ErrorActionPreference = "Stop"
trap { Write-Host "!! Restart FAILED: $_" -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $root

Write-Host "=== Restart services ===" -ForegroundColor Cyan
# 先杀看门狗窗口再杀 python(2026-07-26 修): 只杀 python 会留下旧看门狗循环,
# 10 秒后旧代码复活 + 下面再起一套新看门狗 = 同账户两个 runner 双跑。
# Redirect inside cmd, not PS: under EAP=Stop, PS 5.1 turns taskkill's stderr
# ("process/window not found" - normal when nothing was running) into a fatal error.
cmd /c 'taskkill /F /FI "WINDOWTITLE eq MT5 Bridge*" >nul 2>&1'
cmd /c 'taskkill /F /FI "WINDOWTITLE eq MT5 Runner*" >nul 2>&1'
cmd /c 'taskkill /F /FI "WINDOWTITLE eq MT5 self-test*" >nul 2>&1'
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
