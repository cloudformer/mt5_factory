# MT5 Factory - Windows worker 一键初始化
# 用法(管理员 PowerShell):
#   .\setup.ps1               # Python + 依赖 + 防火墙 + 开机自启(bridge/runner)
#   .\setup.ps1 -InstallMT5   # 额外静默安装 MT5 终端
param([switch]$InstallMT5)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path (Split-Path -Parent $root) "env\.dev.env"   # 与 Linux 共用的统一配置
$port = 8020
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^MT5_PORT=(\d+)'
    if ($m) { $port = [int]$m.Matches.Groups[1].Value }
}

Write-Host "=== [1/6] Python ===" -ForegroundColor Cyan
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
python --version

Write-Host "=== [2/6] Python 依赖 ===" -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
python -m pip install -r "$root\requirements.txt" --quiet

Write-Host "=== [3/6] MT5 终端 ===" -ForegroundColor Cyan
if ($InstallMT5) {
    $installer = "$env:TEMP\mt5setup.exe"
    Invoke-WebRequest "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $installer
    Start-Process $installer -ArgumentList "/auto" -Wait
    Write-Host "MT5 已安装"
} else {
    Write-Host "跳过 (需要时: .\setup.ps1 -InstallMT5)"
}

Write-Host "=== [4/6] 配置文件 ===" -ForegroundColor Cyan
if (-not (Test-Path $envFile)) {
    Copy-Item "$envFile.example" $envFile
    Write-Host "!! 已从模板生成 env\.dev.env — 建议直接用 Linux 上配置好的同一份文件覆盖 !!" -ForegroundColor Yellow
    Write-Host "!! 至少要填 DOCKER_COMPOSE_HOST (Linux VM 的 IP) 和 BRIDGE_API_KEY !!" -ForegroundColor Yellow
} else {
    Write-Host "使用 $envFile"
}

Write-Host "=== [5/6] 防火墙 ===" -ForegroundColor Cyan
if (-not (Get-NetFirewallRule -DisplayName "MT5 Bridge" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "MT5 Bridge" -Direction Inbound -LocalPort $port -Protocol TCP -Action Allow | Out-Null
}
Write-Host "入站端口 $port 已放行"

Write-Host "=== [6/6] 开机自启 (计划任务) ===" -ForegroundColor Cyan
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)
foreach ($task in @(
    @{Name = "MT5Bridge"; Bat = "$root\start_bridge.bat"},
    @{Name = "MT5Runner"; Bat = "$root\start_runner.bat"}
)) {
    $action = New-ScheduledTaskAction -Execute $task.Bat -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName $task.Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "计划任务 $($task.Name) 已注册"
}

Write-Host ""
Write-Host "完成! 启动: .\start_bridge.bat 和 .\start_runner.bat (或注销重登自动拉起)" -ForegroundColor Green
Write-Host "验证: curl http://localhost:$port/health" -ForegroundColor Green
