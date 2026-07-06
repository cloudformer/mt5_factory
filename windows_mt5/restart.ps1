# MT5 Factory - Windows worker 重启服务 + 自检 (不更新代码)
# 用法: powershell -ExecutionPolicy Bypass -File .\restart.ps1
$ErrorActionPreference = "Stop"
trap { Write-Host "!! 重启失败: $_" -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $root

Write-Host "=== 重启服务 ===" -ForegroundColor Cyan
foreach ($t in "MT5Bridge", "MT5Runner") {
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
}
# 专用 worker 机: 确保旧 python 进程退干净
taskkill /F /IM python.exe 2>$null | Out-Null
Start-Sleep -Seconds 2
foreach ($t in "MT5Bridge", "MT5Runner") {
    Start-ScheduledTask -TaskName $t
}

Write-Host "=== 自检 ===" -ForegroundColor Cyan
$port = 8020
$m = Select-String -Path "$repo\env\.dev.env" -Pattern '^MT5_PORT=(\d+)' -ErrorAction SilentlyContinue
if ($m) { $port = [int]$m.Matches.Groups[1].Value }
$health = $null
foreach ($i in 1..12) {
    Start-Sleep -Seconds 5
    try { $health = Invoke-RestMethod "http://localhost:$port/health" -TimeoutSec 3; break } catch { }
}
if ($null -eq $health) {
    Write-Host "!! bridge 60秒内未响应 — 手动运行 start_bridge.bat 查看报错" -ForegroundColor Red
    exit 1
}
Write-Host "bridge: $($health.status)" -ForegroundColor $(if ($health.status -eq 'healthy') { 'Green' } else { 'Yellow' })
Write-Host "重启完成" -ForegroundColor Green
