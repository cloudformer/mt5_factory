# MT5 Factory - Windows worker 一键初始化 + 启动 (每台新VM跑一次, 克隆VM无需再跑)
# 推荐入口: 双击 setup.bat (自动提权+绕过执行策略+窗口保持)
# 手动用法(管理员 PowerShell):
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 [-InstallMT5]
param([switch]$InstallMT5)

$ErrorActionPreference = "Stop"

function Pause-Exit($code) {
    Write-Host ""
    Read-Host "按回车关闭窗口"
    exit $code
}

trap {
    Write-Host ""
    Write-Host "!! 初始化失败于: $($_.InvocationInfo.ScriptLineNumber) 行" -ForegroundColor Red
    Write-Host "!! 错误: $_" -ForegroundColor Red
    Write-Host "!! 修复后重跑本脚本即可 (所有步骤可安全重复执行)" -ForegroundColor Red
    Pause-Exit 1
}

# 管理员检查 (防火墙/计划任务需要)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "!! 需要管理员权限 — 请双击 setup.bat (会自动提权), 或用管理员 PowerShell 运行" -ForegroundColor Red
    Pause-Exit 1
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path (Split-Path -Parent $root) "env\.dev.env"   # 与 Linux 共用的统一配置
$port = 8020
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^MT5_PORT=(\d+)'
    if ($m) { $port = [int]$m.Matches.Groups[1].Value }
}

Write-Host "=== [1/7] Python ===" -ForegroundColor Cyan
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    } else {
        # winget 不存在 (Windows Server / 老版 Win10): 直接从 python.org 下载静默安装
        Write-Host "winget 不可用, 从 python.org 直接安装 Python..." -ForegroundColor Yellow
        $pyExe = "$env:TEMP\python-installer.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $pyExe
        Start-Process $pyExe -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1" -Wait
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
python --version

Write-Host "=== [2/7] Python 依赖 ===" -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
python -m pip install -r "$root\requirements.txt" --quiet

Write-Host "=== [3/7] MT5 终端 ===" -ForegroundColor Cyan
if ($InstallMT5) {
    $installer = "$env:TEMP\mt5setup.exe"
    Invoke-WebRequest "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $installer
    Start-Process $installer -ArgumentList "/auto" -Wait
    Write-Host "MT5 已安装"
} else {
    Write-Host "跳过 (需要时: setup.ps1 -InstallMT5)"
}

Write-Host "=== [4/7] 配置文件 ===" -ForegroundColor Cyan
if (-not (Test-Path $envFile)) {
    Copy-Item "$envFile.example" $envFile
    Write-Host "env\.dev.env 不存在, 已从模板生成 (建议之后用 Linux 上配置好的同一份覆盖)" -ForegroundColor Yellow
}
# 缺关键配置时当场问答补齐, 自动写回 env, 不中断流程
if (Select-String -Path $envFile -Pattern '^DOCKER_COMPOSE_HOST=(127\.|$)') {
    $ip = Read-Host "DOCKER_COMPOSE_HOST 未配置 — 请输入 Linux VM (docker compose) 的 IP"
    if (-not $ip) { Write-Host "!! 必须提供 Linux VM 的 IP" -ForegroundColor Red; exit 1 }
    (Get-Content $envFile) -replace '^DOCKER_COMPOSE_HOST=.*', "DOCKER_COMPOSE_HOST=$ip" |
        Set-Content $envFile -Encoding UTF8
    Write-Host "已写入 DOCKER_COMPOSE_HOST=$ip"
}
if (Select-String -Path $envFile -Pattern '^BRIDGE_API_KEY=(change_me|$)') {
    Write-Host "提示: BRIDGE_API_KEY 还是默认值 — 需与 Linux 侧一致才能通过鉴权" -ForegroundColor Yellow
}
Write-Host "使用 $envFile"

Write-Host "=== [5/7] 防火墙 ===" -ForegroundColor Cyan
if (-not (Get-NetFirewallRule -DisplayName "MT5 Bridge" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "MT5 Bridge" -Direction Inbound -LocalPort $port -Protocol TCP -Action Allow | Out-Null
}
Write-Host "入站端口 $port 已放行"

Write-Host "=== [6/7] 开机自启 (计划任务) ===" -ForegroundColor Cyan
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

Write-Host "=== [7/7] 启动并自检 ===" -ForegroundColor Cyan
Start-ScheduledTask -TaskName "MT5Bridge"
Start-ScheduledTask -TaskName "MT5Runner"
Write-Host "等待 bridge 就绪 (自动拉起 MT5 终端)..."
$health = $null
foreach ($i in 1..18) {
    Start-Sleep -Seconds 5
    try {
        $health = Invoke-RestMethod "http://localhost:$port/health" -TimeoutSec 3
        break
    } catch { }
}
if ($null -eq $health) {
    Write-Host "!! bridge 90秒内未响应 — 手动运行 start_bridge.bat 查看报错" -ForegroundColor Red
    Pause-Exit 1
}
if ($health.status -eq "healthy") {
    Write-Host "bridge: healthy | MT5 已连接 账户=$($health.login) @ $($health.server)" -ForegroundColor Green
} else {
    Write-Host "bridge: 已运行, 但 MT5 未登录账户" -ForegroundColor Yellow
    Write-Host "  → 在 env\.dev.env 填 MT5_LOGIN/PASSWORD/SERVER 后重跑, 或到 web Workers 页下发账户" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "完成! 本机将自动出现在 web Workers 页面 (约1分钟内)" -ForegroundColor Green
Write-Host "开机自启已配置, 之后无需任何手动操作" -ForegroundColor Green
Pause-Exit 0
