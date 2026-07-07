@echo off
title MT5 Bridge
rem MT5 Bridge (watchdog loop: restarts 10s after a crash)
cd /d %~dp0
:loop
echo [%date% %time%] starting bridge...
python bridge\main.py
echo [%date% %time%] bridge exited (code %errorlevel%), restart in 10s...
timeout /t 10 /nobreak >nul
goto loop
