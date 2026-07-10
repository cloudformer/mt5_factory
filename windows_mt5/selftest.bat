@echo off
title MT5 worker self-test
rem One-shot boot self-test (startup shortcut runs this after bridge/runner).
rem Waits for services, tests port/MT5/quotes/order/recon, writes selftest_result.json.
rem Result also shows on the bridge status page - no need to watch this window.
cd /d %~dp0
python selftest.py
if errorlevel 1 (
  echo.
  echo Self-test FAILED - window kept open. Details also on http://localhost:8020/
  pause
)
