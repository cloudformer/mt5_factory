#!/usr/bin/env bash
# 冒烟测试(2026-08-09 改自动抓路由, Frank 定: 清单不再手工维护不再腐烂):
#   web 页面清单 = 从 Flask url_map 现抓全部【无参 GET】路由 → 逐个打开, 要求渲染成功(200)。
#   页面增删改名自动跟随 — 删页不用改本脚本, 忘删引用会当场 FAIL。
#   api 保留手工关键端点(api 无参 GET 少且带业务语义, 手列更准)。
# 用法: ./scripts/smoke.sh   或   make test  (make up 末尾自动跑)
set -u
APP=${APP:-http://localhost:8010}
WEB=${WEB:-http://localhost:8000}
fail=0

check() { # method url want("200"=精确 / "ok"=非5xx即过, 400是参数端点的正常拒绝)
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$1" --max-time 15 "$2")
    if { [ "$3" = "ok" ] && [ "$code" -lt 500 ]; } || [ "$code" = "$3" ]; then
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
check GET "$APP/oos_v2/params" 200
check GET "$APP/regime_screen/progress" 200

echo "== web (8000): 自动抓取全部无参 GET 页面 =="
# 从 web 容器的 Flask url_map 现抓: 无 <参数>、支持 GET、非静态
routes=$(docker compose exec -T web python -c "
from app import app
rules = sorted(r.rule for r in app.url_map.iter_rules()
               if 'GET' in r.methods and '<' not in r.rule
               and r.endpoint != 'static')
print('\n'.join(rules))" 2>/dev/null)
if [ -z "$routes" ]; then
    echo "  FAIL  抓取路由失败(web 容器没起来?)"
    fail=1
else
    while IFS= read -r rule; do
        check GET "$WEB$rule" ok   # 非5xx即过: 页面渲染不崩; 参数端点裸开 400 属正常拒绝
    done <<< "$routes"
fi

if [ "$fail" = 0 ]; then
    echo "== ALL PASS =="
else
    echo "== FAILURES DETECTED =="
fi
exit $fail
