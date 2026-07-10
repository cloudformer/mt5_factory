@echo off
title MT5 Bridge
rem MT5 Bridge (watchdog loop: restarts 10s after a crash)
cd /d %~dp0
rem boot self-test rides along (one-shot, outside the watchdog loop):
rem waits for services then tests the full chain; result -> status page :8020
start "MT5 self-test" /min selftest.bat
:loop
echo [%date% %time%] starting bridge...
python bridge\main.py
echo [%date% %time%] bridge exited (code %errorlevel%), restart in 10s...
rem ping-sleep instead of timeout: timeout errors out without an interactive
rem console, turning this watchdog into a tight crash loop
ping -n 11 127.0.0.1 >nul
goto loop
