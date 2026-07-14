"""策略组页面: 列表(index) / 生成+MQ5转化(generate_page) / 分析(analysis, 骨架) / 状态流转
UI 拆分(2026-07-13): 生成=进货(偶发), 列表=日常主战场, 各自成页; 导航挂「策略▾」下拉。"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("strategies", __name__, url_prefix="/strategies")

TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"]


@bp.get("/")
def index():
    """策略列表排名(唯一工作台): 全部策略(含未回测, 成绩为空沉底) + 成绩/评分/健壮性
    + 筛选(品种/券商/状态/多条件)/搜索/排名模板。数据走 /backtest/top(LEFT JOIN 版)。"""
    a = request.args
    symbol = a.get("symbol") or None
    broker = a.get("broker") or None
    status = a.get("status") or None
    q_field = a.get("q_field") or "name"
    q_text = a.get("q_text") or None
    min_trades = a.get("min_trades", 0, type=int)
    filters = {k: a.get(k, type=float)
               for k in ("min_win_rate", "min_pf", "max_dd", "min_robust")}
    positive = a.get("positive") == "1"
    rank = a.get("rank") or ""  # 排名模板名, 空=默认(净点数)
    results, rank_templates, brokers, symbols = [], [], [], []
    try:
        cfg = api.get("/config")["config"]
        rank_templates = cfg.get("ranking_templates", [])
        params = {"min_trades": min_trades,
                  "limit": cfg.get("backtest_batch_limit", 500)}
        for k, v in (("symbol", symbol), ("broker", broker), ("status", status)):
            if v:
                params[k] = v
        params.update({k: v for k, v in filters.items() if v is not None})
        if positive:
            params["positive_only"] = "true"
        if rank:
            params["rank_template"] = rank
        if q_text:  # 服务端搜索: 策略名模糊 / ID·周期·状态精准
            params["q_field"] = q_field
            params["q_text"] = q_text
        results = api.get("/backtest/top", **params)["results"]
        syms = api.get("/symbols")["symbols"]
        symbols = [s["symbol"] for s in syms if s.get("download")]
        brokers = sorted({s["broker"] for s in syms if s.get("broker")})
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("strategies.html", results=results, symbol=symbol, broker=broker,
                           status=status, min_trades=min_trades, q_field=q_field, q_text=q_text,
                           filters=filters, positive=positive, rank=rank,
                           rank_templates=rank_templates, brokers=brokers, symbols=symbols)


@bp.get("/generate")
def generate_page():
    """策略生成 + MQ5 转化(造新策略的入口)"""
    templates, mq5_imports, default_symbols = {}, [], ""
    try:
        templates = api.get("/strategies/templates")["templates"]
        mq5_imports = api.get("/strategies/mq5")["imports"]
        # 品种默认值从主档取(download=✓), 不写死 — 登记/删品种自动跟着变
        default_symbols = ",".join(
            s["symbol"] for s in api.get("/symbols")["symbols"] if s.get("download"))
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("strategy_generate.html", templates=templates,
                           mq5_imports=mq5_imports, timeframes=TIMEFRAMES,
                           default_symbols=default_symbols)


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
