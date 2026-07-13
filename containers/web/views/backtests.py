"""回测页: 默认成本配置 / 触发批量回测 / 结果排名"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("backtests", __name__, url_prefix="/backtests")


@bp.get("/")
def index():
    symbol = request.args.get("symbol") or None
    broker = request.args.get("broker") or None
    q_field = request.args.get("q_field") or "name"
    q_text = request.args.get("q_text") or None
    min_trades = request.args.get("min_trades", 0, type=int)  # 默认0=全显示; 想过滤过拟合再调高
    results, bt, costs, brokers, symbols, orphans = [], {}, {}, [], [], []
    try:
        params = {"min_trades": min_trades, "limit": 200}  # 前端分页展示, 多取一些
        if symbol:
            params["symbol"] = symbol
        if broker:
            params["broker"] = broker
        if q_text:  # 服务端搜索(查库): 策略名模糊 / ID·周期·状态精准
            params["q_field"] = q_field
            params["q_text"] = q_text
        results = api.get("/backtest/top", **params)["results"]
        bt = api.get("/backtest/status")
        costs = api.get("/config")["config"].get("backtest_costs", {})
        # 两个筛选下拉的选项从库里拉 (货币对/券商), 默认全部; 与 worker 无关
        syms = api.get("/symbols")["symbols"]
        symbols = [s["symbol"] for s in syms if s.get("download")]
        brokers = sorted({s["broker"] for s in syms if s.get("broker")})
        # 孤儿策略(品种已删、跑不了): 亮出来供清理
        orphans = api.get("/strategies/orphans")["orphans"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("backtests.html", results=results, bt=bt, costs=costs,
                           symbol=symbol, broker=broker, min_trades=min_trades,
                           q_field=q_field, q_text=q_text,
                           brokers=brokers, symbols=symbols, orphans=orphans)


@bp.get("/status")
def status():
    """回测进度 JSON — 供页面轮询自动刷新进度条(跑完前端整页刷新看结果)"""
    try:
        return api.get("/backtest/status")
    except api.ApiError as e:
        return {"running": False, "error": str(e)}


@bp.post("/archive-orphans")
def archive_orphans():
    """把品种已删的孤儿策略批量归档(可逆)"""
    try:
        r = api.post("/strategies/orphans/archive", {})
        flash(f"已归档 {r['archived']} 条孤儿策略(品种已删)", "ok")
    except api.ApiError as e:
        flash(f"归档失败: {e}", "error")
    return redirect(url_for("backtests.index"))


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
    payload = {"limit": request.form.get("limit", 500, type=int),
               "slippage_points": request.form.get("slippage_points", type=float),
               "commission_points": request.form.get("commission_points", type=float)}
    # 两个筛选: 货币对 / 券商, 空=全部
    if request.form.get("symbol"):
        payload["symbol"] = request.form["symbol"].strip().upper()
    if request.form.get("broker"):
        payload["broker"] = request.form["broker"]
    if request.form.get("cross_symbol"):  # 跨品种验证(乙)
        payload["cross_symbol"] = True
    if request.form.get("scope") == "untested":  # 范围: 全部(默认) / 仅未测试(补漏)
        payload["untested_only"] = True
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
