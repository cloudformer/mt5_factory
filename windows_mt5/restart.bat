@echo off
rem MT5 Factory - restart worker services (just double-click)
rem 普通用户即可(2026-07-26 去掉 UAC 提权): 杀自己的 python / 经 explorer 起服务不需要管理员。
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0restart.ps1"
