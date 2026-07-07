# MT5 Factory - Windows worker setup + start (run once per new VM; cloned VMs skip this)
# Recommended entry: double-click setup.bat (auto-elevate + bypass policy + keep window open)
# Manual usage (admin PowerShell):
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 [-SkipMT5]
# Everything (Python, deps, MT5 terminal) is installed by default; already-present pieces are detected and skipped.
param([switch]$SkipMT5)

$ErrorActionPreference = "Stop"
# Old Windows PowerShell defaults to TLS 1.0 - python.org/mql5.com downloads would fail
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Progress bar slows Invoke-WebRequest downloads by an order of magnitude
$ProgressPreference = "SilentlyContinue"

function Pause-Exit($code) {
    Write-Host ""
    Read-Host "Press Enter to close"
    exit $code
}

trap {
    Write-Host ""
    Write-Host "!! Setup FAILED at line: $($_.InvocationInfo.ScriptLineNumber)" -ForegroundColor Red
    Write-Host "!! Error: $_" -ForegroundColor Red
    Write-Host "!! Fix the issue and re-run this script (all steps are safe to repeat)" -ForegroundColor Red
    Pause-Exit 1
}

# $ErrorActionPreference only catches PowerShell errors - native exes (python/git/pip) return
# a nonzero exit code without throwing, so failures there would silently pass. Check explicitly.
function Assert-LastExitCode($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

# Admin check (required by firewall rule / scheduled tasks)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "!! Administrator required - double-click setup.bat (auto-elevates), or run from an admin PowerShell" -ForegroundColor Red
    Pause-Exit 1
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $root
$envFile = Join-Path $repoRoot "env\.dev.env"   # shared config with Linux side
$port = 8020
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^MT5_PORT=(\d+)'
    if ($m) { $port = [int]$m.Matches.Groups[1].Value }
}

Write-Host "=== [1/8] Defender exclusions ===" -ForegroundColor Cyan
# Corporate AV/EDR routinely blocks or locks files during the downloads+silent-installs below
# (Python/MT5 installers, pip). Built-in Defender: exclude the repo + TEMP so that stops happening.
# Third-party EDR (CrowdStrike/SentinelOne/etc) isn't controllable from here - IT must exclude it.
if (Get-Command Add-MpPreference -ErrorAction SilentlyContinue) {
    try {
        Add-MpPreference -ExclusionPath $repoRoot, $env:TEMP -ErrorAction Stop
        Write-Host "Excluded from Windows Defender: $repoRoot, $env:TEMP"
    } catch {
        Write-Host "Could not add Defender exclusions ($_) - installs below may get blocked" -ForegroundColor Yellow
    }
} else {
    Write-Host "Windows Defender not present (3rd-party AV/EDR managed by IT? ask them to exclude $repoRoot and `$env:TEMP)" -ForegroundColor Yellow
}

Write-Host "=== [2/8] Python ===" -ForegroundColor Cyan

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

Refresh-Path

$python = Get-Command python -ErrorAction SilentlyContinue

if (-not $python) {
    Write-Host "Python not found, installing Python 3.12..." -ForegroundColor Yellow

    # Try winget first, but treat its failure as "not installed" and fall through
    # to the direct installer (winget breaks in odd ways: no msstore agreement,
    # outdated sources, group policy blocks...)
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.12 -e `
            --accept-source-agreements --accept-package-agreements
        Refresh-Path
    }

    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Host "Downloading Python installer from python.org..." -ForegroundColor Yellow
        $pyInstaller = "$env:TEMP\python-installer.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" `
            -OutFile $pyInstaller
        $proc = Start-Process $pyInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1" `
            -Wait -PassThru
        if ($proc.ExitCode -ne 0) { throw "Python installer exited with code $($proc.ExitCode)" }
    }

    # PATH updates land in the registry with a delay - poll until python resolves
    $retry = 0
    while (-not (Get-Command python -ErrorAction SilentlyContinue) -and $retry -lt 30) {
        Start-Sleep -Seconds 2
        Refresh-Path
        $retry++
    }
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python installation failed. python.exe not found in PATH."
}

# The MetaTrader5 package only ships 64-bit wheels - a stray 32-bit Python first in
# PATH would fail at 'pip install' with a baffling "no matching distribution" error
$bits = python -c "import struct; print(struct.calcsize('P') * 8)"
if ($bits -ne "64") {
    throw "python in PATH is $bits-bit at $((Get-Command python).Source) - MetaTrader5 requires 64-bit Python; remove/reorder the 32-bit one"
}

python --version
python -m pip --version
Assert-LastExitCode "python/pip sanity check"

Write-Host "=== [3/8] Python dependencies ===" -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
Assert-LastExitCode "pip upgrade"
python -m pip install -r "$root\requirements.txt" --quiet
Assert-LastExitCode "pip install -r requirements.txt"

Write-Host "=== [4/8] MT5 terminal ===" -ForegroundColor Cyan

function Find-MT5Terminal {
    $found = @()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base -and (Test-Path $base)) {
            $found += Get-ChildItem -Path $base -Filter terminal64.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path "$env:APPDATA\MetaQuotes\Terminal") {
        $found += Get-ChildItem -Path "$env:APPDATA\MetaQuotes\Terminal" -Filter terminal64.exe -Recurse -Depth 1 -ErrorAction SilentlyContinue
    }
    return $found | Select-Object -First 1
}

$mt5Path = $null
if ($SkipMT5) {
    Write-Host "Skipped (-SkipMT5)"
} else {
    $existing = Find-MT5Terminal
    if ($existing) {
        Write-Host "Already installed: $($existing.FullName)"
        $mt5Path = $existing.FullName
    } else {
        Write-Host "Not found, installing..." -ForegroundColor Yellow
        $installer = "$env:TEMP\mt5setup.exe"
        Invoke-WebRequest "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $installer
        $proc = Start-Process $installer -ArgumentList "/auto" -Wait -PassThru
        $installed = Find-MT5Terminal
        if ($proc.ExitCode -ne 0 -or -not $installed) {
            throw "MT5 installer exited with code $($proc.ExitCode) and terminal64.exe still not found"
        }
        $mt5Path = $installed.FullName
        Write-Host "MT5 installed"
    }
}

Write-Host "=== [5/8] Config file ===" -ForegroundColor Cyan
if (-not (Test-Path $envFile)) {
    Copy-Item "$envFile.example" $envFile
    Write-Host "env\.dev.env created from template (better: copy the configured one from the Linux side)" -ForegroundColor Yellow
}
if ($mt5Path) {
    # mt5.initialize() auto-detection is unreliable (esp. after a silent /auto install) and
    # fails with "IPC initialize failed, MetaTrader 5 x64 not found" even though it IS installed -
    # pin the path we just found/installed so bridge/runner pass it explicitly.
    $envLines = Get-Content $envFile -Encoding UTF8
    if ($envLines -match '^MT5_PATH=\s*$') {
        $envLines -replace '^MT5_PATH=.*', "MT5_PATH=$mt5Path" | Set-Content $envFile -Encoding UTF8
        Write-Host "Saved MT5_PATH=$mt5Path"
    } elseif (-not ($envLines -match '^MT5_PATH=')) {
        Add-Content $envFile -Encoding UTF8 -Value "MT5_PATH=$mt5Path"
        Write-Host "Saved MT5_PATH=$mt5Path"
    }
}
# Interactive fill for missing critical values (written to env only after you confirm)
if (Select-String -Path $envFile -Pattern '^DOCKER_COMPOSE_HOST=(127\.|$)') {
    $ip = Read-Host "DOCKER_COMPOSE_HOST is not set - enter the Linux VM (docker compose) IP"
    if (-not $ip) { Write-Host "!! Linux VM IP is required" -ForegroundColor Red; Pause-Exit 1 }
    # explicit UTF8: the env file has Chinese comments; default ANSI read would corrupt them
    (Get-Content $envFile -Encoding UTF8) -replace '^DOCKER_COMPOSE_HOST=.*', "DOCKER_COMPOSE_HOST=$ip" |
        Set-Content $envFile -Encoding UTF8
    Write-Host "Saved DOCKER_COMPOSE_HOST=$ip"
}
if (Select-String -Path $envFile -Pattern '^BRIDGE_API_KEY=(change_me|$)') {
    Write-Host "Note: BRIDGE_API_KEY is still the default - it must match the Linux side" -ForegroundColor Yellow
}
Write-Host "Using $envFile"

Write-Host "=== [6/8] Firewall ===" -ForegroundColor Cyan
if (-not (Get-NetFirewallRule -DisplayName "MT5 Bridge" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "MT5 Bridge" -Direction Inbound -LocalPort $port -Protocol TCP -Action Allow | Out-Null
}
Write-Host "Inbound port $port allowed"

Write-Host "=== [7/8] Auto-start (startup shortcuts) ===" -ForegroundColor Cyan
# 自启用"启动文件夹快捷方式"而非计划任务: 登录自启 = 和用户双击 bat 完全同一种启动方式。
# 计划任务环境里 python 对 MT5 终端的 IPC 附着实测始终 (-10005, 'IPC timeout'),
# 双击路径实测可连; 崩溃自愈由 bat 内的看门狗循环负责, 不依赖计划任务的重启策略。
foreach ($t in "MT5Bridge", "MT5Runner") {   # 清掉旧方案的计划任务, 避免双重启动
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Write-Host "Removed old scheduled task $t"
    }
}
$startup = [Environment]::GetFolderPath("Startup")
$shell = New-Object -ComObject WScript.Shell
foreach ($item in @(
    @{Name = "MT5Bridge"; Bat = "$root\start_bridge.bat"},
    @{Name = "MT5Runner"; Bat = "$root\start_runner.bat"}
)) {
    $lnk = $shell.CreateShortcut("$startup\$($item.Name).lnk")
    $lnk.TargetPath = $item.Bat
    $lnk.WorkingDirectory = $root
    $lnk.Save()
    Write-Host "Startup shortcut $($item.Name) created"
}

Write-Host "=== [8/8] Restart + self-check ===" -ForegroundColor Cyan
# Dedicated worker VM: stray python processes ARE the old bridge/runner.
# Redirect inside cmd, not PS: under EAP=Stop, PS 5.1 turns taskkill's stderr
# ("process not found" - the normal case on first install) into a fatal error.
cmd /c "taskkill /F /IM python.exe >nul 2>&1"
Start-Sleep -Seconds 2
# 必须经 explorer 启动: 本脚本是管理员权限, 直接 Start-Process 会把提升权限传给子进程,
# 提升的 python 连不上普通权限的 MT5 终端; 经 explorer = 普通权限, 与双击/开机自启完全一致
foreach ($bat in "start_bridge.bat", "start_runner.bat") {
    explorer.exe "$root\$bat"
}
Write-Host "Waiting for bridge (it will auto-launch the MT5 terminal)..."
$health = $null
foreach ($i in 1..18) {
    Start-Sleep -Seconds 5
    try {
        $health = Invoke-RestMethod "http://localhost:$port/health" -TimeoutSec 3
        break
    } catch { }
}
if ($null -eq $health) {
    Write-Host "!! bridge did not respond within 90s - run start_bridge.bat manually to see the error" -ForegroundColor Red
    Pause-Exit 1
}
$runnerAlive = $health.runner -and $health.runner.alive
Write-Host ("runner: " + $(if ($runnerAlive) { "running" } else { "not running yet (watchdog restarts it; check start_runner.bat window if it stays down)" }))
if ($health.status -eq "healthy") {
    Write-Host "bridge: healthy | MT5 connected, account=$($health.login) @ $($health.server)" -ForegroundColor Green
} else {
    Write-Host "bridge: running, but MT5 is not connected/logged in yet" -ForegroundColor Yellow
    Write-Host "  -> freshly installed MT5? check the terminal window for first-run dialogs (EULA/account wizard) and close them" -ForegroundColor Yellow
    Write-Host "  -> no account? set MT5_LOGIN/PASSWORD/SERVER in env\.dev.env and re-run, or push one from the web Workers page" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Done! This machine will appear on the web Workers page within ~1 minute" -ForegroundColor Green
Write-Host "Auto-start is configured - no manual steps needed from now on" -ForegroundColor Green
Pause-Exit 0
