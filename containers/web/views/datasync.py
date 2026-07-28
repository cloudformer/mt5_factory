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
    # 两层参考分(2026-07-28 Frank 定, 仅参考不做门禁): 假的东西谈不上好坏 —
    # 及格层=③区分度60分(真伪: 波幅比30+t值30); 好坏层40分(①持续性20 ②覆盖12 ④不冗余8);
    # 闸门: ③两个门槛都过线才计好坏层; ③未算=不出总分(没验真伪就没有分)。
    score = None
    st_ = (data or {}).get("stats") or {}
    if st_.get("days"):
        cap = lambda v: max(0.0, min(1.0, v))  # noqa: E731
        d = st_["dwell"]
        q1 = (cap(d["long"] / 20) + cap(d["short"] / 5) + cap(d["vol"] / 5)
              + cap(st_["combo_median"] / 3)
              + cap(60 / st_["flips_per_year"] if st_["flips_per_year"] else 1)) / 5 * 20
        q2 = (cap(st_["cov_min"] / 5) + cap(35 / st_["cov_max"] if st_["cov_max"] else 1)) / 2 * 12
        a = st_["agree_max"]
        q4 = (1.0 if a <= 75 else (cap((90 - a) / 15) if a < 90 else 0.0)) * 8
        quality = round(q1 + q2 + q4)   # 好坏层 0~40
        dt = (data or {}).get("distinct") or {}
        if dt.get("vol_ratio") is not None:
            validity = round(cap(dt["vol_ratio"] / 1.5) * 30 + cap(abs(dt["trend_t"] or 0) / 2) * 30)
            passed = dt["vol_ratio"] >= 1.5 and dt.get("trend_t") is not None and abs(dt["trend_t"]) >= 2
            score = {"validity": validity, "quality": quality, "passed": passed,
                     "total": validity + (quality if passed else 0)}
        else:   # ③未算: 只亮好坏层预览, 不出总分
            score = {"validity": None, "quality": quality, "passed": None, "total": None}
        score["parts"] = {"③波幅比(30)+t值(30)": score["validity"],
                          "①持续性(20)": round(q1), "②覆盖(12)": round(q2), "④不冗余(8)": round(q4)}
    # 色带按季分行(2026-07-28 Frank 定: 月分行太窄, 3个月一行, 默认近一年=4行): [(季度, [rows])]
    band_months = []
    for r in (data or {}).get("rows", [])[-365:]:
        q = f"{r['date'][:4]} Q{(int(r['date'][5:7]) - 1) // 3 + 1}"
        if not band_months or band_months[-1][0] != q:
            band_months.append((q, []))
        band_months[-1][1].append(r)
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
                           full=full, data=data, cell_lines=cell_lines, neighbors=neighbors,
                           score=score, band_months=band_months)


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
