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
    # 多条件过滤(1.1): 空=不限, 全部服务端查库
    filters = {k: request.args.get(k, type=float)
               for k in ("min_win_rate", "min_pf", "max_dd", "min_robust")}
    positive = request.args.get("positive") == "1"
    results, bt, costs, brokers, symbols, orphans = [], {}, {}, [], [], []
    try:
        params = {"min_trades": min_trades, "limit": 200}  # 前端分页展示, 多取一些
        if symbol:
            params["symbol"] = symbol
        if broker:
            params["broker"] = broker
        params.update({k: v for k, v in filters.items() if v is not None})
        if positive:
            params["positive_only"] = "true"
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
                           q_field=q_field, q_text=q_text, filters=filters, positive=positive,
                           brokers=brokers, symbols=symbols, orphans=orphans)


@bp.get("/status")
def status():
    """回测进度 JSON — 供页面轮询自动刷新进度条(跑完前端整页刷新看结果)"""
    try:
        return api.get("/backtest/status")
    except api.ApiError as e:
        return {"running": False, "error": str(e)}


@bp.get("/plan")
def plan():
    """运行预览 JSON — 表单选项一变就刷新"将回测 N×M=K 次"(透传 api /backtest/plan)"""
    try:
        return api.get("/backtest/plan",
                       **{k: v for k, v in request.args.items() if v not in (None, "")})
    except api.ApiError as e:
        return {"strategies": 0, "symbols_per": 1, "runs": 0, "error": str(e)}


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
        if request.form.get("batch_limit", "").strip():  # 单批上限(防失控保护, 可配置)
            api.put("/config/backtest_batch_limit",
                    {"value": int(request.form["batch_limit"])})
        flash("回测参数已保存", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"保存失败: {e}", "error")
    # 成本表单在「配置」页(也可能其它入口), 保存后回来源页
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/run")
def run():
    """启动批量回测。选策略二选一: 按筛选(货币对/券商/范围) 或 按ID(点名)。
    成本不再从本表单传 — api 自动取系统默认(上方"默认成本"区块是唯一修改处)。"""
    payload = {}
    if request.form.get("mode") == "ids":  # 按 ID 点名, 忽略筛选
        ids = [s.strip() for s in request.form.get("strategy_ids", "").split(",") if s.strip()]
        if not ids:
            flash("按ID模式需要填策略ID(逗号分隔)", "error")
            return redirect(url_for("backtests.index"))
        try:
            payload["strategy_ids"] = [int(s) for s in ids]
        except ValueError:
            flash("策略ID必须是数字, 逗号分隔", "error")
            return redirect(url_for("backtests.index"))
    else:  # 按筛选圈一批
        if request.form.get("symbol"):
            payload["symbol"] = request.form["symbol"].strip().upper()
        if request.form.get("broker"):
            payload["broker"] = request.form["broker"]
        if request.form.get("scope") == "untested":  # 范围: 全部(默认) / 仅未测试(补漏)
            payload["untested_only"] = True
    if request.form.get("cross_symbol"):  # 跨品种验证(乙)
        payload["cross_symbol"] = True
    try:
        result = api.post("/backtest/run", payload)
        flash(f"回测已启动: {result['total']} 个策略 (成本: {result['costs']})", "ok")
    except api.ApiError as e:
        if "no strategies matched" in str(e):  # 没有匹配不算故障, 说人话
            flash("没有匹配的策略可回测 — 范围=仅未测试时说明全部都测过了(想重测切'全部')", "error")
        else:
            flash(f"启动回测失败: {e}", "error")
    return redirect(url_for("backtests.index"))
