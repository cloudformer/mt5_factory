#!/usr/bin/env bash
# API 冒烟测试: 全量检查只读端点 (不触发写操作)
# 用法: ./scripts/smoke.sh   或   make test
set -u
APP=${APP:-http://localhost:8010}
WEB=${WEB:-http://localhost:8000}
fail=0

check() { # method url want
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" --max-time 10 "$2")
    if [ "$code" = "$3" ]; then
        printf '  PASS  %-4s %-45s %s\n' "$1" "$2" "$code"
    else
        printf '  FAIL  %-4s %-45s got=%s want=%s\n' "$1" "$2" "$code" "$3"
        fail=1
    fi
}

echo "== api (8010) =="
check GET "$APP/health" 200
check GET "$APP/hosts" 200
check GET "$APP/config" 200
check GET "$APP/syncdata/status" 200
check GET "$APP/symbols" 200
check GET "$APP/strategies/templates" 200
check GET "$APP/strategies/status?limit=1" 200
check GET "$APP/backtest/status" 200
check GET "$APP/backtest/top" 200

echo "== web (8000) =="
check GET "$WEB/healthz" 200
check GET "$WEB/" 200
check GET "$WEB/strategies/" 200
check GET "$WEB/strategies/generate" 200
check GET "$WEB/strategies/analysis" 200
check GET "$WEB/strategies/quality" 200
check GET "$WEB/backtests/" 200
check GET "$WEB/workers/" 200
check GET "$WEB/datasync/" 200
check GET "$WEB/demo/" 200
check GET "$WEB/live/" 200

if [ "$fail" = 0 ]; then
    echo "== ALL PASS =="
else
    echo "== FAILURES DETECTED =="
fi
exit $fail
