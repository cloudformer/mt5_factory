"""回测页: 默认成本配置 / 触发批量回测 / 结果排名"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("backtests", __name__, url_prefix="/backtests")


@bp.get("/")
def index():
    """策略回测页(纯执行): 批量/单策略回测 + 进度 + 孤儿警告。结果排名在「策略列表排名」。"""
    bt, costs, brokers, symbols, orphans, templates = {}, {}, [], [], [], []
    try:
        costs = api.get("/config")["config"].get("backtest_costs", {})
        bt = api.get("/backtest/status")
        # 运行表单筛选下拉的选项从库里拉 (货币对/券商), 默认全部; 与 worker 无关
        syms = api.get("/symbols")["symbols"]
        symbols = [s["symbol"] for s in syms if s.get("download")]
        brokers = sorted({s["broker"] for s in syms if s.get("broker")})
        templates = sorted(api.get("/strategies/templates")["templates"].keys())
        # 孤儿策略(品种已删、跑不了): 亮出来供清理
        orphans = api.get("/strategies/orphans")["orphans"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("backtests.html", bt=bt, costs=costs,
                           brokers=brokers, symbols=symbols, orphans=orphans,
                           templates=templates)


@bp.get("/status")
def status():
    """回测进度 JSON — 供页面轮询自动刷新进度条(跑完前端整页刷新看结果)"""
    try:
        return api.get("/backtest/status")
    except api.ApiError as e:
        return {"running": False, "error": str(e)}


@bp.post("/cancel")
def cancel():
    """取消当前批次(透传 api): jobs 化后重启 api 会续跑, 取消必须显式"""
    try:
        return api.post("/backtest/cancel", {})
    except api.ApiError as e:
        return {"error": str(e)}, 502


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
        if request.form.get("oos_split", "").strip():  # OOS 训练占比(0~1); 换比例必全量收敛
            api.put("/config/backtest_oos_split",
                    {"value": float(request.form["oos_split"])})
        flash("回测参数已保存", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"保存失败: {e}", "error")
    # 成本表单在「配置·回测参数」页, 保存后回来源页(兜底回该页)
    return redirect(request.referrer or url_for("symbols.backtest_params"))


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
        if request.form.get("template"):
            payload["template"] = request.form["template"]
        if request.form.get("symbol"):
            payload["symbol"] = request.form["symbol"].strip().upper()
        if request.form.get("broker"):
            payload["broker"] = request.form["broker"]
        if request.form.get("status"):  # 状态维度(可选): 如热层 DEMO,LIVE 每日刷新
            payload["status"] = request.form["status"]
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
