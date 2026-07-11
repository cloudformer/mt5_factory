# MT5 Factory - update worker: pull latest code + deps, then restart + self-check
# Usage: powershell -ExecutionPolicy Bypass -File .\update.ps1
$ErrorActionPreference = "Stop"
trap { Write-Host "!! Update FAILED: $_" -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $root

function Assert-LastExitCode($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

Write-Host "=== Stop services first ===" -ForegroundColor Cyan
# 必须先停 python: 运行中的 bridge/runner 会锁住 .py 文件, git pull 覆盖失败(Windows 文件锁,
# 是"更新看似成功、版本号却不变"的元凶)。停了再拉, 拉完 restart.ps1 再起。
# 经 cmd 重定向: EAP=Stop 下 taskkill 的 "进程不存在" stderr 会被 PS 当致命错误
cmd /c "taskkill /F /IM python.exe >nul 2>&1"
Start-Sleep -Seconds 2

Write-Host "=== Update code ===" -ForegroundColor Cyan
if ((Test-Path "$repo\.git") -and (Get-Command git -ErrorAction SilentlyContinue)) {
    git -C $repo pull
    Assert-LastExitCode "git pull"
} else {
    Write-Host "No git repo or git not installed - skipped pull (copy files manually, then run this script)" -ForegroundColor Yellow
}
python -m pip install -r "$root\requirements.txt" --quiet
Assert-LastExitCode "pip install -r requirements.txt"

& "$root\restart.ps1"
