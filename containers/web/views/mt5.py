"""MT5 流水页: 从选中 worker 实时透传持仓 + 历史成交明细 (不落库, 人工核对用)

链路 web → api → bridge → MT5, 看到的就是券商侧原始数据;
web 端只做两件事: magic → 策略名归因, 枚举值翻译成中文。
"""
from datetime import datetime

from flask import Blueprint, flash, render_template, request

import api_client as api

bp = Blueprint("mt5", __name__, url_prefix="/mt5")

ENTRY_CN = {"in": "开仓", "out": "平仓", "inout": "反手", "out_by": "对冲平"}
REASON_CN = {"sl": "止损", "tp": "止盈", "expert": "程序", "manual": "手动",
             "mobile": "手机", "web": "网页", "so": "强平"}
SMOKE_MAGIC = 999999


def _who(magic: int, magic_map: dict) -> str:
    if magic == SMOKE_MAGIC:
        return "下单测试"
    if magic in magic_map:
        return magic_map[magic]
    if magic == 0:
        return "手动/其他"
    # magic 规则(全系统不变量, 三处依赖: api分配/runner下单/此处兜底) = 100000 + 策略id;
    # 999999=下单测试。规则可用到 id=899998 才会撞测试号 — 到那天再整体迁基数, 现在不动。
    if 100000 < magic < SMOKE_MAGIC:
        return f"策略 #{magic - 100000}"
    return f"未知 magic {magic}"


@bp.get("/")
def index():
    days = request.args.get("days", 30, type=int)
    hosts, data, magic_map = [], None, {}
    try:
        hosts = [h for h in api.get("/hosts")["hosts"] if h["enabled"]]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    # 两级选择: demo / live / 其他(未指派, 也可能登着账户)
    groups = [("demo", [h for h in hosts if h["runner"] == "demo"]),
              ("live", [h for h in hosts if h["runner"] == "live"]),
              ("其他", [h for h in hosts if not h["runner"]])]
    host_id = request.args.get("host_id", type=int) or next(
        (g[1][0]["id"] for g in groups if g[1]), None)  # 默认第一台 demo 主机
    # 选中 worker 登录的券商(server) — 整页流水都来自这一家, 放页头
    sel = next((h for h in hosts if h["id"] == host_id), None)
    broker = ((sel or {}).get("last_health") or {}).get("server") if sel else None
    if host_id:
        try:
            data = api.get(f"/hosts/{host_id}/trades", days=days)
            # magic→策略名映射: 上限给足(策略库会超500); 超出仍有 _who 的"策略 #id"兜底
            strategies = api.get("/strategies/status", limit=5000)["strategies"]
            magic_map = {s["magic_number"]: s["name"]
                         for s in strategies if s["magic_number"]}
        except api.ApiError as e:
            flash(f"流水获取失败: {e}", "error")
    if data:
        for p in data["positions"]:
            p["time_fmt"] = datetime.fromtimestamp(p["time"]).strftime("%m-%d %H:%M:%S")
            p["who"] = _who(p["magic"], magic_map)
        for d in data["deals"]:
            d["time_fmt"] = datetime.fromtimestamp(d["time"]).strftime("%m-%d %H:%M:%S")
            d["entry_cn"] = ENTRY_CN.get(d["entry"], d["entry"])
            # 原因只对平仓有意义; 开仓一律程序 → 冗余不显示。
            # 平仓若是"程序"触发 = 测试单(策略平仓只会 SL/TP, 不主动平) → 显示"测试"
            if d["entry"] == "in":
                d["reason_cn"] = "—"
            elif d["reason"] == "expert":
                d["reason_cn"] = "测试"
            else:
                d["reason_cn"] = REASON_CN.get(d["reason"], d["reason"])
            d["who"] = "入金/出金" if d["type"] == "balance" else _who(d["magic"], magic_map)
    return render_template("mt5.html", groups=groups, host_id=host_id, days=days,
                           data=data, broker=broker,
                           worker_name=(sel or {}).get("name"),
                           worker_role=(sel or {}).get("runner"))
