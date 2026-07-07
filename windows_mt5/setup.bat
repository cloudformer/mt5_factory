@echo off
rem MT5 Factory - Windows worker 初始化入口 (双击即可)
rem 自动: 请求管理员权限 + 绕过执行策略 + 窗口保持打开
rem 需要安装 MT5 终端时: 在 cmd 里运行  setup.bat -InstallMT5
cd /d %~dp0
set "PSARGS=-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0setup.ps1\""
if not "%~1"=="" set "PSARGS=%PSARGS% %*"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '%PSARGS%'"
