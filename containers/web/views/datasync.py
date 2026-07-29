"""下载页: 触发历史数据同步 + 每品种覆盖情况(只读)

品种在『品种』页维护(唯一数据源 symbols 表); 本页只负责把数据拉下来。
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("datasync", __name__, url_prefix="/datasync")


# 周期层用途标注(下载页勾选项显示用): 谁吃这层数据一眼可见
TF_ROLE = {"M1": "回测", "D1": "regime"}


def _two_tier_score(st_, dt) -> dict | None:
    """两层参考分(2026-07-29 Frank 修订, 仅参考不做门禁): 总分 = 两层直接相加 —
    及格层=③区分度60分(真伪: 波幅比30+t值30, 权重大); 好坏层40分(①20 ②12 ④8)。
    ③门槛过没过只作标注(passed, 徽章颜色)不再压分; ③未算=不出总分(没验真伪就没有分)。
    细则未来再探讨。单品种记分卡与候选族对比页共用; 格龄用按天加权值(尺子改良)。"""
    if not st_.get("days"):
        return None
    cap = lambda v: max(0.0, min(1.0, v))  # noqa: E731
    d = st_["dwell"]
    q1 = (cap(d["long"] / 20) + cap(d["short"] / 5) + cap(d["vol"] / 5)
          + cap(st_["combo_median"] / 3)
          + cap(60 / st_["flips_per_year"] if st_["flips_per_year"] else 1)) / 5 * 20
    q2 = (cap(st_["cov_min"] / 5) + cap(35 / st_["cov_max"] if st_["cov_max"] else 1)) / 2 * 12
    a = st_["agree_max"]
    q4 = (1.0 if a <= 75 else (cap((90 - a) / 15) if a < 90 else 0.0)) * 8
    quality = round(q1 + q2 + q4)   # 好坏层 0~40
    if (dt or {}).get("vol_ratio") is not None:
        validity = round(cap(dt["vol_ratio"] / 1.5) * 30 + cap(abs(dt["trend_t"] or 0) / 2) * 30)
        passed = (dt["vol_ratio"] >= 1.5 and dt.get("trend_t") is not None
                  and abs(dt["trend_t"]) >= 2)
        score = {"validity": validity, "quality": quality, "passed": passed,
                 "total": validity + quality}   # 两层直接相加(2026-07-29 修订)
    else:   # ③未算: 只亮好坏层预览, 不出总分
        score = {"validity": None, "quality": quality, "passed": None, "total": None}
    score["parts"] = {"③波幅比(30)+t值(30)": score["validity"],
                      "①持续性(20)": round(q1), "②覆盖(12)": round(q2), "④不冗余(8)": round(q4)}
    return score


@bp.get("/")
def index():
    data = {"symbols": [], "orphans": [], "sync": {}, "hosts": [], "timeframes": [],
            "wp": {}, "batch_tfs": []}
    try:
        s = api.get("/symbols")
        data["symbols"], data["orphans"] = s["symbols"], s.get("orphans", [])
        data["sync"] = api.get("/syncdata/status")
        data["hosts"] = [h for h in api.get("/hosts")["hosts"]
                         if h["enabled"] and h["download"]]
        # 本次同步的周期层勾选项 = 配置 download_timeframes(唯一源, 配置页可改)
        cfg = api.get("/config")["config"]
        tfs = cfg.get("download_timeframes") or []
        data["timeframes"] = [{"tf": t, "role": TF_ROLE.get(t, "")} for t in tfs]
        data["wp"] = cfg.get("worker_params") or {}   # 下载节流两键的现值(节流表单用)
        # 正在跑的批实际包含哪些层(从任务名现算, 带·后缀=高周期): 跑批时显示它而非勾选框 —
        # 勾选框默认全勾, 刷新后看起来像"M1也在跑"(2026-07-29 Frank 指出的误导显示 bug)
        data["batch_tfs"] = sorted({x.split("·")[1] if "·" in x else "M1"
                                    for x in (data["sync"].get("symbols") or [])})
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
    tfs = request.form.getlist("tf")   # 本次只下勾中的层; 全不勾在 api 侧被明确拒绝
    try:
        api.post("/syncdata", {"timeframes": tfs})
        flash(f"同步已启动({'+'.join(tfs)})", "ok")
    except api.ApiError as e:
        flash(f"启动同步失败: {e}", "error")
    return redirect(url_for("datasync.index"))


@bp.post("/throttle")
def throttle():
    """保存下载节流(worker_params 里的 dl_rest_bars/dl_rest_secs, 2026-07-29 Frank 定):
    每拉 N 根歇 S 秒 — 首灌深历史防 CPU 打满; 值走 announce 下发, 约1分钟生效(含跑着的任务)"""
    try:
        cur = api.get("/config")["config"].get("worker_params") or {}
        api.put("/config/worker_params", {"value": {**cur,
                "dl_rest_bars": int(request.form["dl_rest_bars"]),
                "dl_rest_secs": int(request.form["dl_rest_secs"])}})
        flash("下载节流已保存 — worker 下次报到(约1分钟)领取, 正在跑的任务从下一批生效", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"保存失败: {e}", "error")
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
    score = _two_tier_score((data or {}).get("stats") or {}, (data or {}).get("distinct") or {})
    # 色带按半年分行(2026-07-29 Frank 定: 半年一行, 默认显示3年, 跨度下拉前端切片):
    # 全历史都建 [(半年标签, [rows])], 模板给每行标序号, JS 只显示最后 N 行 — 零请求切换
    band_months = []
    for r in (data or {}).get("rows", []):
        h = f"{r['date'][:4]} {'上' if int(r['date'][5:7]) <= 6 else '下'}"
        if not band_months or band_months[-1][0] != h:
            band_months.append((h, []))
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


@bp.get("/regime/eval")
def regime_eval():
    """候选口径族对比(v2.5 评定流程"记分卡"): 8 候选 × 全部下载品种现算(只读不落库)。
    每格 = 两层参考分; 每候选给 中位/最差(评定按整体, 不因单品种贴线否决); 展开看四标准中位值"""
    data, rows = None, []
    try:
        data = api.get("/regime/evaluate", timeout=300)
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    import statistics
    for cand in (data or {}).get("candidates", []):
        cells, crit = {}, {"dl": [], "ds": [], "dv": [], "cm": [], "fy": [],
                           "cmin": [], "cmax": [], "vr": [], "tt": [], "ag": []}
        for sym, v in cand["per_symbol"].items():
            st_, dt = v.get("stats") or {}, v.get("distinct") or {}
            sc = _two_tier_score(st_, dt)
            cells[sym] = sc
            if st_.get("days"):
                crit["dl"].append(st_["dwell"]["long"]); crit["ds"].append(st_["dwell"]["short"])
                crit["dv"].append(st_["dwell"]["vol"]); crit["cm"].append(st_["combo_median"])
                crit["fy"].append(st_["flips_per_year"])
                crit["cmin"].append(st_["cov_min"]); crit["cmax"].append(st_["cov_max"])
                crit["ag"].append(st_["agree_max"])
            if dt.get("vol_ratio") is not None:
                crit["vr"].append(dt["vol_ratio"])
                if dt.get("trend_t") is not None:
                    crit["tt"].append(abs(dt["trend_t"]))
        totals = [c["total"] for c in cells.values() if c and c["total"] is not None]
        med = lambda xs: round(statistics.median(xs), 1) if xs else None  # noqa: E731
        rows.append({"label": cand["label"], "cells": cells,
                     "median": med(totals), "worst": min(totals) if totals else None,
                     # 校准用: 每指标 中位(最差) — 门槛定歪一眼可见
                     "crit": {k: (med(v), (min(v) if k not in ("fy", "cmax", "ag") else max(v)) if v else None)
                              for k, v in crit.items()}})
    rows.sort(key=lambda r: -(r["median"] or -1))
    return render_template("regime_eval.html", data=data, rows=rows,
                           symbols=(data or {}).get("symbols", []),
                           skipped=(data or {}).get("skipped", []))


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
