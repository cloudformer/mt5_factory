@echo off
rem MT5 Bridge (看门狗: 崩溃 10s 自动重启)
cd /d %~dp0
:loop
echo [%date% %time%] starting bridge...
python bridge\main.py
echo [%date% %time%] bridge exited (code %errorlevel%), restart in 10s...
timeout /t 10 /nobreak >nul
goto loop
