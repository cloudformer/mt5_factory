@echo off
title MT5 Bridge
rem MT5 Bridge (watchdog loop: restarts 10s after a crash)
cd /d %~dp0
rem boot self-test rides along (one-shot at startup): waits for services then tests
rem the full chain; result -> status page :8020
start "MT5 self-test" /min selftest.bat
:loop
echo [%date% %time%] starting bridge...
python bridge\main.py
rem 远程重启: /restart 写了 restart.flag 并让 bridge 主动退出。此时 python 已退出=文件已解锁,
rem 在这里(看门狗两次循环之间)连 runner 一起重启并重跑自检 —— 不与看门狗抢锁, 无进程互杀。
if exist restart.flag (
  del restart.flag
  echo [%date% %time%] restart requested: stopping runner + rerunning self-test...
  cmd /c "taskkill /F /IM python.exe >nul 2>&1"
  ping -n 3 127.0.0.1 >nul
  start "MT5 self-test" /min selftest.bat
  goto loop
)
echo [%date% %time%] bridge exited (code %errorlevel%), restart in 10s...
rem ping-sleep instead of timeout: timeout errors out without an interactive
rem console, turning this watchdog into a tight crash loop
ping -n 11 127.0.0.1 >nul
goto loop
