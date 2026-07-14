"""策略组页面: 列表(index) / 生成+MQ5转化(generate_page) / 分析(analysis, 骨架) / 状态流转
UI 拆分(2026-07-13): 生成=进货(偶发), 列表=日常主战场, 各自成页; 导航挂「策略▾」下拉。"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("strategies", __name__, url_prefix="/strategies")

TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"]


@bp.get("/")
def index():
    """策略列表(日常主战场)"""
    status = request.args.get("status") or None
    symbol = request.args.get("symbol") or None
    strategies = []
    try:
        params = {k: v for k, v in {"status": status, "symbol": symbol, "limit": 200}.items() if v}
        strategies = api.get("/strategies/status", **params)["strategies"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("strategies.html", strategies=strategies,
                           status=status, symbol=symbol)


@bp.get("/generate")
def generate_page():
    """策略生成 + MQ5 转化(造新策略的入口)"""
    templates, mq5_imports = {}, []
    try:
        templates = api.get("/strategies/templates")["templates"]
        mq5_imports = api.get("/strategies/mq5")["imports"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("strategy_generate.html", templates=templates,
                           mq5_imports=mq5_imports, timeframes=TIMEFRAMES)


@bp.get("/analysis")
def analysis():
    """策略分析(骨架页, 内容随 OOS/对账/评价加权逐步落进来)"""
    return render_template("strategy_analysis.html")


@bp.post("/generate")
def generate():
    try:
        result = api.post("/strategies/generate", {
            "template": request.form["template"],
            "symbols": [s.strip().upper() for s in request.form["symbols"].split(",") if s.strip()],
            "timeframe": request.form["timeframe"],
            "mode": request.form.get("mode", "random"),
            "count": request.form.get("count", 50, type=int),
        })
        msg = f"已生成 {result['created']} 个策略实例"
        if result.get("skipped"):
            msg += f"（跳过 {result['skipped']} 个已存在的相同组合）"
        flash(msg, "ok" if result["created"] else "error")
    except (api.ApiError, KeyError) as e:
        flash(f"生成失败: {e}", "error")
    return redirect(url_for("strategies.index", status="CANDIDATE"))


@bp.post("/<int:strategy_id>/backtest")
def run_backtest(strategy_id: int):
    """单策略回测 (成本用系统默认; 结果在回测页排名可见)"""
    try:
        api.post("/backtest/run", {"strategy_ids": [strategy_id]})
        flash(f"策略 #{strategy_id} 回测已启动, 结果见回测页", "ok")
    except api.ApiError as e:
        flash(f"回测启动失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))


@bp.post("/mq5")
def mq5_submit():
    try:
        result = api.post("/strategies/mq5", {
            "name": request.form["name"].strip(),
            "source": request.form["source"],
        })
        flash(f"MQ5 已提交待评估 (id={result['id']})", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"提交失败: {e}", "error")
    return redirect(url_for("strategies.generate_page"))  # MQ5 转化表在生成页


@bp.post("/<int:strategy_id>/status")
def set_status(strategy_id: int):
    is_fetch = request.headers.get("X-Requested-With") == "fetch"  # AJAX 原地更新, 不刷新页面
    try:
        result = api.post(f"/strategies/{strategy_id}/status",
                          {"status": request.form["status"]})
        if is_fetch:
            return result
        flash(f"{result['name']} → {result['status']}"
              + (f" (magic={result['magic_number']})" if result.get("magic_number") else ""), "ok")
    except (api.ApiError, KeyError) as e:
        if is_fetch:
            return {"error": str(e)}, 400
        flash(f"状态修改失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))
