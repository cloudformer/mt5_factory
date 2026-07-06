# MT5 Factory - Windows worker 更新: 拉最新代码 + 装依赖 → 重启服务 + 自检
# 用法: powershell -ExecutionPolicy Bypass -File .\update.ps1
$ErrorActionPreference = "Stop"
trap { Write-Host "!! 更新失败: $_" -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $root

Write-Host "=== 更新代码 ===" -ForegroundColor Cyan
if (Test-Path "$repo\.git") {
    git -C $repo pull
} else {
    Write-Host "非 git 目录, 跳过拉取 (手动覆盖文件后再跑本脚本即可)" -ForegroundColor Yellow
}
python -m pip install -r "$root\requirements.txt" --quiet

& "$root\restart.ps1"
