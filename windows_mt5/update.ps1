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
# 顺序铁律(2026-07-26 修): 先杀看门狗窗口(cmd 循环), 再杀 python —
# 只杀 python 的话, start_bridge/start_runner 的看门狗 10 秒后会把旧代码重新拉起:
# 轻则文件锁回来 pull/pip 失败, 重则 restart 再起一套 = 新旧两个 runner 同账户双跑。
# 经 cmd 重定向: EAP=Stop 下 taskkill 的 "进程/窗口不存在" stderr 会被 PS 当致命错误
cmd /c 'taskkill /F /FI "WINDOWTITLE eq MT5 Bridge*" >nul 2>&1'
cmd /c 'taskkill /F /FI "WINDOWTITLE eq MT5 Runner*" >nul 2>&1'
cmd /c 'taskkill /F /FI "WINDOWTITLE eq MT5 self-test*" >nul 2>&1'
cmd /c "taskkill /F /IM python.exe >nul 2>&1"
Start-Sleep -Seconds 2

Write-Host "=== Update code ===" -ForegroundColor Cyan
if ((Test-Path "$repo\.git") -and (Get-Command git -ErrorAction SilentlyContinue)) {
    # worker is a stateless clone (iron rule 5): no local edits are legitimate here.
    # Discard any local changes (runtime-generated files once tracked by mistake,
    # e.g. selftest_result.json / worker_params.json) so pull never conflicts.
    git -C $repo checkout -- .
    git -C $repo pull
    Assert-LastExitCode "git pull"
} else {
    Write-Host "No git repo or git not installed - skipped pull (copy files manually, then run this script)" -ForegroundColor Yellow
}
# 普通用户运行(2026-07-26 去 UAC): Python 装在系统目录时 pip 会因无写权限失败 —
# 自动退到 --user(用户 site-packages 在 sys.path 里优先于系统目录, 新依赖照样生效)
python -m pip install -r "$root\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip 写系统目录无权限(普通用户) - 改用 --user 重试" -ForegroundColor Yellow
    python -m pip install -r "$root\requirements.txt" --quiet --user
    Assert-LastExitCode "pip install --user (仍失败: 右键'以管理员身份运行'本脚本一次)"
}

& "$root\restart.ps1"
