@echo off
title MT5 self-test
rem Double-click to run (NORMAL privilege, do NOT run as admin).
cd /d %~dp0
echo BEFORE running, make sure:
echo   1. MT5 terminal is OPEN and LOGGED IN to your demo account (normal double-click, not admin)
echo   2. this window is NORMAL privilege (not admin)
echo.
pause
echo.
python check.py
echo.
pause
