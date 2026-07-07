"""回测页: 默认成本配置 / 触发批量回测 / 结果排名"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("backtests", __name__, url_prefix="/backtests")


@bp.get("/")
def index():
    symbol = request.args.get("symbol") or None
    min_trades = request.args.get("min_trades", 30, type=int)
    results, bt, costs = [], {}, {}
    try:
        params = {"min_trades": min_trades, "limit": 50}
        if symbol:
            params["symbol"] = symbol
        results = api.get("/backtest/top", **params)["results"]
        bt = api.get("/backtest/status")
        costs = api.get("/config")["config"].get("backtest_costs", {})
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("backtests.html", results=results, bt=bt, costs=costs,
                           symbol=symbol, min_trades=min_trades)


@bp.post("/costs")
def save_costs():
    """保存系统默认成本 (config 表, 运行表单会预填它)"""
    try:
        spread = request.form.get("spread_points", "").strip()
        api.put("/config/backtest_costs", {"value": {
            "slippage_points": float(request.form["slippage_points"]),
            "commission_points": float(request.form["commission_points"]),
            "spread_points": float(spread) if spread else None,
        }})
        flash("默认成本已保存", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("backtests.index"))


@bp.post("/run")
def run():
    payload = {"status": request.form.get("status", "CANDIDATE"),
               "limit": request.form.get("limit", 500, type=int),
               "slippage_points": request.form.get("slippage_points", type=float),
               "commission_points": request.form.get("commission_points", type=float)}
    if request.form.get("symbol"):
        payload["symbol"] = request.form["symbol"].strip().upper()
    if request.form.get("spread_points", "").strip():
        payload["spread_points"] = float(request.form["spread_points"])
    ids = [s.strip() for s in request.form.get("strategy_ids", "").split(",") if s.strip()]
    if ids:
        try:
            payload["strategy_ids"] = [int(s) for s in ids]
        except ValueError:
            flash("策略ID必须是数字, 逗号分隔", "error")
            return redirect(url_for("backtests.index"))
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        result = api.post("/backtest/run", payload)
        flash(f"回测已启动: {result['total']} 个策略 (成本: {result['costs']})", "ok")
    except api.ApiError as e:
        flash(f"启动回测失败: {e}", "error")
    return redirect(url_for("backtests.index"))
