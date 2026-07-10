@echo off
title MT5 order-path smoke test
rem Double-click to run (NORMAL privilege, do NOT run as admin).
rem Opens ONE minimal order on the DEMO account and closes it immediately.
rem Refuses to run on a real account. Optional arg = symbol (default XAUUSD).
cd /d %~dp0
echo BEFORE running, make sure:
echo   1. MT5 terminal is OPEN and LOGGED IN to your DEMO account
echo   2. the 'Algo Trading' toolbar button is ON
echo   3. market is open (weekday)
echo.
echo This will BUY the minimum lot and close it at once - costs one spread.
echo.
pause
echo.
python ordertest.py %1
echo.
pause
