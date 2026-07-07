@echo off
rem MT5 Factory - Windows worker 初始化入口 (双击即可)
rem 自动: 请求管理员权限 + 绕过执行策略 + 窗口保持打开
rem 需要安装 MT5 终端时: setup.bat -InstallMT5
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','\"%~dp0setup.ps1\"','%*'"
