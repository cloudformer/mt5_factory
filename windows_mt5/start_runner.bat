@echo off
rem 实时执行 Runner (看门狗: 崩溃 10s 自动重启)
cd /d %~dp0
:loop
echo [%date% %time%] starting runner...
python runner\main.py
echo [%date% %time%] runner exited (code %errorlevel%), restart in 10s...
timeout /t 10 /nobreak >nul
goto loop
