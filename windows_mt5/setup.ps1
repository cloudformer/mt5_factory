# MT5 Factory - Windows worker setup + start (run once per new VM; cloned VMs skip this)
# Recommended entry: double-click setup.bat (auto-elevate + bypass policy + keep window open)
# Manual usage (admin PowerShell):
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 [-InstallMT5]
param([switch]$InstallMT5)

$ErrorActionPreference = "Stop"

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

# Admin check (required by firewall rule / scheduled tasks)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "!! Administrator required - double-click setup.bat (auto-elevates), or run from an admin PowerShell" -ForegroundColor Red
    Pause-Exit 1
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path (Split-Path -Parent $root) "env\.dev.env"   # shared config with Linux side
$port = 8020
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^MT5_PORT=(\d+)'
    if ($m) { $port = [int]$m.Matches.Groups[1].Value }
}

Write-Host "=== [1/7] Python ===" -ForegroundColor Cyan

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

Refresh-Path

$python = Get-Command python -ErrorAction SilentlyContinue

if (-not $python) {

    Write-Host "Python not found, installing Python 3.12..." -ForegroundColor Yellow

    if (Get-Command winget -ErrorAction SilentlyContinue) {

        winget install `
            --id Python.Python.3.12 `
            -e `
            --accept-source-agreements `
            --accept-package-agreements

    }
    else {

        Write-Host "winget unavailable, downloading Python installer..." -ForegroundColor Yellow

        $pyInstaller = "$env:TEMP\python-installer.exe"

        Invoke-WebRequest `
            "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" `
            -OutFile $pyInstaller

        Start-Process `
            $pyInstaller `
            -ArgumentList `
            "/quiet InstallAllUsers=1 PrependPath=1" `
            -Wait
    }


    # ˢ�»�������
    Refresh-Path

    # �ȴ���װ��ɲ����¼��
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


python --version
python -m pip --version

Write-Host "=== [2/7] Python dependencies ===" -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
python -m pip install -r "$root\requirements.txt" --quiet

Write-Host "=== [3/7] MT5 terminal ===" -ForegroundColor Cyan
if ($InstallMT5) {
    $installer = "$env:TEMP\mt5setup.exe"
    Invoke-WebRequest "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $installer
    Start-Process $installer -ArgumentList "/auto" -Wait
    Write-Host "MT5 installed"
} else {
    Write-Host "Skipped (run: setup.bat -InstallMT5 if needed)"
}

Write-Host "=== [4/7] Config file ===" -ForegroundColor Cyan
if (-not (Test-Path $envFile)) {
    Copy-Item "$envFile.example" $envFile
    Write-Host "env\.dev.env created from template (better: copy the configured one from the Linux side)" -ForegroundColor Yellow
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

Write-Host "=== [5/7] Firewall ===" -ForegroundColor Cyan
if (-not (Get-NetFirewallRule -DisplayName "MT5 Bridge" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "MT5 Bridge" -Direction Inbound -LocalPort $port -Protocol TCP -Action Allow | Out-Null
}
Write-Host "Inbound port $port allowed"

Write-Host "=== [6/7] Auto-start (scheduled tasks) ===" -ForegroundColor Cyan
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)
foreach ($task in @(
    @{Name = "MT5Bridge"; Bat = "$root\start_bridge.bat"},
    @{Name = "MT5Runner"; Bat = "$root\start_runner.bat"}
)) {
    $action = New-ScheduledTaskAction -Execute $task.Bat -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName $task.Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "Scheduled task $($task.Name) registered"
}

Write-Host "=== [7/7] Start + self-check ===" -ForegroundColor Cyan
Start-ScheduledTask -TaskName "MT5Bridge"
Start-ScheduledTask -TaskName "MT5Runner"
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
if ($health.status -eq "healthy") {
    Write-Host "bridge: healthy | MT5 connected, account=$($health.login) @ $($health.server)" -ForegroundColor Green
} else {
    Write-Host "bridge: running, but MT5 has no account logged in" -ForegroundColor Yellow
    Write-Host "  -> set MT5_LOGIN/PASSWORD/SERVER in env\.dev.env and re-run, or push account from the web Workers page" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Done! This machine will appear on the web Workers page within ~1 minute" -ForegroundColor Green
Write-Host "Auto-start is configured - no manual steps needed from now on" -ForegroundColor Green
Pause-Exit 0
