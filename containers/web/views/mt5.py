"""MT5 流水页: 从选中 worker 实时透传持仓 + 历史成交明细 (不落库, 人工核对用)

链路 web → api → bridge → MT5, 看到的就是券商侧原始数据;
web 端只做两件事: magic → 策略名归因, 枚举值翻译成中文。
"""
from datetime import datetime, timedelta

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
    # 时间: 预设(近N天)或自定义起始日; 实时流水只"最近N天到现在", 故自定义只用"从"(to=现在)
    win = request.args.get("win") or request.args.get("days") or "30"  # 兼容旧 days= 链接
    frm = request.args.get("from") or ""
    if win == "custom" and frm:
        try:
            days = max(1, (datetime.now().date() - datetime.strptime(frm, "%Y-%m-%d").date()).days)
        except ValueError:
            days = 30
    else:
        days = int(win) if str(win).isdigit() else 30
    hosts, data, magic_map, presets = [], None, {}, [7, 30, 90]
    try:
        presets = api.get("/config")["config"].get("mt5_trades_days") or [7, 30, 90]
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
    # 账户四卡(2.1): 与 Demo/Live 同源 — runner 落盘 → bridge /health → api 心跳 last_health(~30s)
    runner = ((sel or {}).get("last_health") or {}).get("runner") or {}
    account = ({"host": sel["name"], **runner["account"]}
               if sel and runner.get("account") else None)
    acct_stale = None  # 回传超过3分钟没更新 → 明示是过期快照(过期数据看着和实时一样, 会骗人)
    if account and runner.get("updated"):
        age = datetime.now().timestamp() - runner["updated"]
        if age > 180:
            acct_stale = int(age / 60)
    if data:
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
                           presets=presets, win=win, frm=frm,
                           data=data, broker=broker, account=account, acct_stale=acct_stale,
                           worker_name=(sel or {}).get("name"),
                           worker_role=(sel or {}).get("runner"))


@bp.get("/system")
def system():
    """系统流水: 本地库 trades(持久副本), 按账号 + 时间范围(预设/自定义)查。
    与 Worker 流水(实时拉 MT5)互补 — 这个读库, 不限 90 天、worker 离线也能看。"""
    a = request.args
    account = a.get("account", type=int)
    win = a.get("win") or "30"        # 预设天数 or 'custom'
    frm = a.get("from") or ""
    to = a.get("to") or ""
    presets, accounts, trades, magic_map, cons = [7, 30, 90], [], [], {}, None
    try:
        presets = api.get("/config")["config"].get("mt5_trades_days") or [7, 30, 90]
        # 时间窗(预设/自定义)→ 同一个窗口喂 流水查询 + 一致性核对
        from_iso, to_iso = None, None
        if win == "custom":
            from_iso = frm or None
            to_iso = (to + "T23:59:59") if to else None
        else:
            days = int(win) if str(win).isdigit() else 30
            from_iso = (datetime.now() - timedelta(days=days)).isoformat()
        params = {}
        if account:
            params["account"] = account
        if from_iso:
            params["from_time"] = from_iso
        if to_iso:
            params["to_time"] = to_iso
        data = api.get("/trades/local", **params)
        accounts, trades = data["accounts"], data["trades"]
        strategies = api.get("/strategies/status", limit=5000)["strategies"]
        magic_map = {s["magic_number"]: s["name"] for s in strategies if s["magic_number"]}
        # 一致性核对(本时段 库 vs MT5): 需选定具体账号才能定位其 worker
        if account and from_iso:
            cp = {"account": account, "from_time": from_iso}
            if to_iso:
                cp["to_time"] = to_iso
            cons = api.get("/trades/consistency", **cp)
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    for t in trades:
        t["who"] = magic_map.get(t["magic"]) or _who(t["magic"], {})
    return render_template("mt5_system.html", presets=presets, win=win, frm=frm, to=to,
                           account=account, accounts=accounts, trades=trades, cons=cons)
