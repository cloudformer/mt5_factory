@echo off
title MT5 reconciliation dump (read-only)
rem Double-click to run (NORMAL privilege). Never trades - only reads history.
rem Optional arg = days of history (default 90).
cd /d %~dp0
python recon.py %1
echo.
pause
