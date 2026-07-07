@echo off
rem Trading Runner (watchdog loop: restarts 10s after a crash)
cd /d %~dp0
:loop
echo [%date% %time%] starting runner...
python runner\main.py
echo [%date% %time%] runner exited (code %errorlevel%), restart in 10s...
timeout /t 10 /nobreak >nul
goto loop
