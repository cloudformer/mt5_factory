@echo off
rem MT5 Factory - update worker: git pull + deps + restart (just double-click)
rem 普通用户即可(2026-07-26 去掉 UAC 提权): 杀自己的 python / git pull / 起服务都不需要管理员;
rem pip 若因系统目录无权限失败, update.ps1 自动改 --user 重试。只有装机(setup.bat)才要管理员。
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0update.ps1"
