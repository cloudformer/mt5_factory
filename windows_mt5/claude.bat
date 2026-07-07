@echo off
chcp 65001 >nul
title Claude Code Installer

echo =================================
echo       Claude Code Installer
echo =================================


:: 管理员权限
net session >nul 2>&1

if %errorlevel% neq 0 (
    echo Requesting Administrator permission...

    powershell.exe ^
    -NoProfile ^
    -Command "Start-Process '%~f0' -Verb RunAs"

    exit /b
)


echo Administrator OK


powershell.exe ^
-NoProfile ^
-ExecutionPolicy Bypass ^
-File "%~dp0claude.ps1"


echo.
echo =================================
echo Finished
echo =================================

pause