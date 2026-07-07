@echo off
rem MT5 Factory - update worker: git pull + deps + restart (just double-click)
rem Auto: request admin (UAC) + bypass execution policy + keep window open
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0update.ps1\"'"
