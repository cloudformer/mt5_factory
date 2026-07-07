@echo off
title MT5 Runner
rem Trading Runner (watchdog loop: restarts 10s after a crash)
cd /d %~dp0
:loop
echo [%date% %time%] starting runner...
python runner\main.py
echo [%date% %time%] runner exited (code %errorlevel%), restart in 10s...
rem ping-sleep instead of timeout: timeout errors out without an interactive
rem console, turning this watchdog into a tight crash loop
ping -n 11 127.0.0.1 >nul
goto loop
