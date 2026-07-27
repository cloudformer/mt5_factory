"""MT5 流水页(v7.2 #5 单向化, 2026-07-26 与 Frank 定: 功能保留、通道反转):
持仓 = worker 心跳快照(last_health.positions, 每拍覆盖, 零新表零清理, 最多滞后一个心跳);
成交 = 库内已平仓回合(trades 表, #2 心跳推送落库) — api 不再反向连 worker。
web 端只做: magic → 策略名归因, 枚举值翻译成中文。
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
    acct_login = (sel or {}).get("mt5_login")
    if host_id and sel:
        try:
            # v7.2 #5 单向化: 不再反向拉 worker — 持仓读心跳快照, 成交读库(推送落的 trades)
            data = {"days": days,
                    "positions": (sel.get("last_health") or {}).get("positions"),
                    "trades": []}
            if acct_login:
                data["trades"] = api.get(
                    "/trades/local", account=acct_login, include_test="true",
                    from_time=(datetime.now() - timedelta(days=days)).isoformat())["trades"]
            # magic→策略名映射(轻量名册端点, 无 limit — 超库存导致格式不齐的坑已修)
            strategies = api.get("/strategies/names")["strategies"]  # 轻量名册: 全量, 归属列格式统一
            magic_map = {s["magic_number"]: s["name"]
                         for s in strategies if s["magic_number"]}
        except api.ApiError as e:
            flash(f"流水获取失败: {e}", "error")
            data = None
    if data and data["positions"] is not None:
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
        for t in data["trades"]:   # 库内回合: 时间(JSON里是ISO串)格式化 + 原因翻中文 + magic 归因
            for k in ("entry_time", "exit_time"):
                try:
                    t[k + "_fmt"] = datetime.fromisoformat(t[k]).strftime("%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    t[k + "_fmt"] = "—"
            t["reason_cn"] = ("测试" if t.get("close_reason") == "expert"
                              else REASON_CN.get(t.get("close_reason"), t.get("close_reason") or "—"))
            t["who"] = _who(t.get("magic") or 0, magic_map)
    return render_template("mt5.html", groups=groups, host_id=host_id, days=days,
                           presets=presets, win=win, frm=frm,
                           data=data, broker=broker, account=account, acct_stale=acct_stale,
                           snapshot_at=(sel or {}).get("last_heartbeat"),  # 快照最后更新(相对时间标注)
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
    include_test = a.get("include_test") == "1"   # 默认不含: 过滤下单测试单
    presets, accounts, trades, magic_map = [7, 30, 90], [], [], {}
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
        if include_test:
            params["include_test"] = "true"
        data = api.get("/trades/local", **params)
        accounts, trades = data["accounts"], data["trades"]
        strategies = api.get("/strategies/names")["strategies"]  # 轻量名册: 全量, 归属列格式统一
        magic_map = {s["magic_number"]: s["name"] for s in strategies if s["magic_number"]}
        # 一致性核对块已删(2026-07-26 v7.2 收口): 由常驻哨兵接管 — 每次成交推送入库后
        # 自检, 缺一笔即 trades_mismatch 事件(Workers 页详情可见), 比手动点核对更早更全
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    for t in trades:
        t["who"] = magic_map.get(t["magic"]) or _who(t["magic"], {})
    # 账号按券商分组(券商→账号 两级下拉); accounts 为 [{account, broker}]
    acct_groups = {}
    for ac in accounts:
        acct_groups.setdefault(ac.get("broker") or "未知券商", []).append(ac["account"])
    return render_template("mt5_system.html", presets=presets, win=win, frm=frm, to=to,
                           account=account, acct_groups=acct_groups, trades=trades,
                           include_test=include_test)
