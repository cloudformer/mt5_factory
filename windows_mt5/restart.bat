@echo off
rem MT5 Factory - restart worker services (just double-click)
rem Auto: request admin (UAC) + bypass execution policy + keep window open
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0restart.ps1\"'"
