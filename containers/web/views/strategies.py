"""策略页: 生成 / 列表 / 状态流转"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("strategies", __name__, url_prefix="/strategies")

TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"]


@bp.get("/")
def index():
    status = request.args.get("status") or None
    symbol = request.args.get("symbol") or None
    strategies, templates = [], {}
    try:
        params = {k: v for k, v in {"status": status, "symbol": symbol, "limit": 200}.items() if v}
        strategies = api.get("/strategies/status", **params)["strategies"]
        templates = api.get("/strategies/templates")["templates"]
    except api.ApiError as e:
        flash(f"app API 不可用: {e}", "error")
    return render_template("strategies.html", strategies=strategies, templates=templates,
                           timeframes=TIMEFRAMES, status=status, symbol=symbol)


@bp.post("/generate")
def generate():
    try:
        result = api.post("/strategies/generate", {
            "template": request.form["template"],
            "symbols": [s.strip().upper() for s in request.form["symbols"].split(",") if s.strip()],
            "timeframe": request.form["timeframe"],
        })
        flash(f"已生成 {result['created']} 个策略实例", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"生成失败: {e}", "error")
    return redirect(url_for("strategies.index", status="CANDIDATE"))


@bp.post("/<int:strategy_id>/status")
def set_status(strategy_id: int):
    try:
        result = api.post(f"/strategies/{strategy_id}/status",
                          {"status": request.form["status"]})
        flash(f"{result['name']} → {result['status']}"
              + (f" (magic={result['magic_number']})" if result.get("magic_number") else ""), "ok")
    except api.ApiError as e:
        flash(f"状态修改失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))
