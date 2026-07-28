"""下载页: 触发历史数据同步 + 每品种覆盖情况(只读)

品种在『品种』页维护(唯一数据源 symbols 表); 本页只负责把数据拉下来。
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("datasync", __name__, url_prefix="/datasync")


@bp.get("/")
def index():
    data = {"symbols": [], "orphans": [], "sync": {}, "hosts": []}
    try:
        s = api.get("/symbols")
        data["symbols"], data["orphans"] = s["symbols"], s.get("orphans", [])
        data["sync"] = api.get("/syncdata/status")
        data["hosts"] = [h for h in api.get("/hosts")["hosts"]
                         if h["enabled"] and h["download"]]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("datasync.html", **data)


@bp.get("/status")
def status():
    """同步进度 JSON — 供页面轮询更新进度条(与回测页同一模式)"""
    try:
        return api.get("/syncdata/status")
    except api.ApiError as e:
        return {"running": False, "error": str(e)}


@bp.post("/run")
def run():
    try:
        api.post("/syncdata")
        flash("同步已启动", "ok")
    except api.ApiError as e:
        flash(f"启动同步失败: {e}", "error")
    return redirect(url_for("datasync.index"))


@bp.get("/regime")
def regime():
    """市场状态 Regime(v2.5 阶段一): 品种时间线演变 + 四标准统计 — 尺子先行, 只看不选。
    full=1 附算区分度(标准③, 重扫M1几秒~几十秒, 口径评定时手动点)"""
    symbol = (request.args.get("symbol") or "").strip().upper() or None
    days = request.args.get("days", 90, type=int)
    full = request.args.get("full", 0, type=int)
    symbols, data = [], None
    try:
        symbols = [s["symbol"] for s in api.get("/symbols")["symbols"] if s.get("download")]
        symbol = symbol or (symbols[0] if symbols else None)
        if symbol:
            data = api.get(f"/regime/{symbol}", days=days, **({"full": 1} if full else {}))
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    # 象限图显示准备: 每格一行"N天 · P%"(稀格标红), 今天所在格 + 海明距离1的三个邻格
    cell_lines, neighbors = {}, []
    if data and data.get("stats", {}).get("cells"):
        st = data["stats"]
        for cell, pct in st["cells"].items():
            d = round(pct * st["days"] / 100)
            cls = ' class="neg"' if pct < 5 else ""
            cell_lines[cell] = [f'<span{cls}>{d} 天 · {pct}%</span>']
        cur = data["current"]["regime"]
        neighbors = [cur[:i] + ("B" if cur[i] == "A" else "A") + cur[i + 1:] for i in range(3)]
    return render_template("regime.html", symbols=symbols, symbol=symbol, days=days,
                           full=full, data=data, cell_lines=cell_lines, neighbors=neighbors)


@bp.post("/regime/rebuild")
def regime_rebuild():
    """按当前口径重建时间线(显式动作, 2026-07-27 Frank 定): 表单带 symbol=只重建该货币,
    带 all=1 重建全部下载品种。覆盖更新不删表, 完成即回 Regime 页看新打分。"""
    symbol = (request.form.get("symbol") or "").strip().upper()
    rebuild_all = request.form.get("all") == "1"
    try:
        r = api.post("/regime/rebuild" + ("" if rebuild_all else f"?symbol={symbol}"),
                     timeout=300)
        fails = {s: v for s, v in r["results"].items() if v != "ok"}
        msg = f"重建完成: {r['ok']}/{r['total']} 个品种成功(口径 {r['params']['long_ma']}/{r['params']['short_ma']})"
        if fails:
            msg += " — 未成功: " + "; ".join(f"{s}: {v}" for s, v in fails.items())
        flash(msg, "ok" if not fails else "error")
    except api.ApiError as e:
        flash(f"重建失败: {e}", "error")
    return redirect(url_for("datasync.regime", symbol=symbol or None))
