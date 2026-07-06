"""回测页: 触发批量回测 / 结果排名"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("backtests", __name__, url_prefix="/backtests")


@bp.get("/")
def index():
    symbol = request.args.get("symbol") or None
    min_trades = request.args.get("min_trades", 30, type=int)
    results, bt = [], {}
    try:
        params = {"min_trades": min_trades, "limit": 50}
        if symbol:
            params["symbol"] = symbol
        results = api.get("/backtest/top", **params)["results"]
        bt = api.get("/backtest/status")
    except api.ApiError as e:
        flash(f"app API 不可用: {e}", "error")
    return render_template("backtests.html", results=results, bt=bt,
                           symbol=symbol, min_trades=min_trades)


@bp.post("/run")
def run():
    payload = {"status": request.form.get("status", "CANDIDATE"),
               "limit": request.form.get("limit", 500, type=int)}
    if request.form.get("symbol"):
        payload["symbol"] = request.form["symbol"].strip().upper()
    try:
        result = api.post("/backtest/run", payload)
        flash(f"回测已启动: {result['total']} 个策略", "ok")
    except api.ApiError as e:
        flash(f"启动回测失败: {e}", "error")
    return redirect(url_for("backtests.index"))
