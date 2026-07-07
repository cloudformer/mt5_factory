@echo off
rem MT5 Factory - Windows worker setup entry (just double-click)
rem Auto: request admin (UAC) + bypass execution policy + keep window open
rem To also install the MT5 terminal, run from cmd:  setup.bat -InstallMT5
cd /d %~dp0
set "PSARGS=-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0setup.ps1\""
if not "%~1"=="" set "PSARGS=%PSARGS% %*"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '%PSARGS%'"
