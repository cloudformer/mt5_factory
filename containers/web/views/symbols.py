"""品种页: 品种主档(唯一数据源)的独立维护界面

品种一切信息只在 symbols 表: 登记(向券商校验)、精度、下载开关、每品种起始日期。
下载/回测/策略生成都从这里读。本页是它唯一的管理入口。
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("symbols", __name__, url_prefix="/symbols")


@bp.get("/")
def index():
    """配置·货币对: 品种主档(登记/列表) — 精度/下载/清空都在「下载」页"""
    symbols = []
    try:
        symbols = api.get("/symbols")["symbols"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("symbols.html", symbols=symbols)


@bp.get("/backtest")
def backtest_params():
    """配置·回测参数: 成本模型 + 单批上限 + OOS 切分"""
    costs, batch_limit, oos_split = {}, 500, 0.7
    try:
        cfg = api.get("/config")["config"]
        costs = cfg.get("backtest_costs", {})
        batch_limit = cfg.get("backtest_batch_limit", 500)
        oos_split = cfg.get("backtest_oos_split", 0.7)
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("config_backtest.html", costs=costs,
                           batch_limit=batch_limit, oos_split=oos_split)


@bp.get("/ranking")
def ranking():
    """配置·排名模板: 四维加权评分模板(增删改)"""
    rank_templates = []
    try:
        rank_templates = api.get("/config")["config"].get("ranking_templates", [])
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("config_ranking.html", rank_templates=rank_templates)


@bp.get("/ai")
def ai():
    """配置·AI 生成器: 外部 AI 服务地址(可选)"""
    ai_url = ""
    try:
        ai_url = api.get("/config")["config"].get("ai_generator_url") or ""
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("config_ai.html", ai_url=ai_url)


@bp.post("/config/ranks")
def save_ranks():
    """保存排名模板(config: ranking_templates)。UI 可增删改:
    删除=勾『删』或清空名称; 新增=填底部空白行; 校验在 api 侧把关"""
    tpls, i = [], 0
    while f"rt_name_{i}" in request.form:
        name = request.form[f"rt_name_{i}"].strip()
        if name and not request.form.get(f"rt_del_{i}"):
            try:
                tpls.append({
                    "name": name,
                    "stable": float(request.form.get(f"rt_stable_{i}") or 0),
                    "profit": float(request.form.get(f"rt_profit_{i}") or 0),
                    "risk": float(request.form.get(f"rt_risk_{i}") or 0),
                    "robust": float(request.form.get(f"rt_robust_{i}") or 0),
                    "min_trades": int(request.form.get(f"rt_mt_{i}") or 0),
                })
            except ValueError:
                flash(f"模板 {name}: 权重/笔数必须是数字", "error")
                return redirect(url_for("symbols.index"))
        i += 1
    try:
        api.put("/config/ranking_templates", {"value": tpls})
        flash(f"排名模板已保存({len(tpls)} 个)", "ok")
    except api.ApiError as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.ranking"))


@bp.post("/config/ai")
def save_ai():
    """保存 AI 生成器地址(config: ai_generator_url; 空=不用)"""
    try:
        api.put("/config/ai_generator_url",
                {"value": request.form.get("ai_generator_url", "").strip()})
        flash("AI 生成器地址已保存", "ok")
    except api.ApiError as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.ai"))


@bp.post("/add")
def add():
    """登记品种: api 会向券商校验存在性并自动取真实精度"""
    try:
        result = api.post("/symbols", {
            "symbol": request.form["symbol"].strip().upper(),
            "data_start": request.form.get("data_start", "2015-01-01").strip(),
        })
        flash(f"{result['symbol']} 已登记 (digits={result['digits']}, point={result['point']})", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"登记失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/<symbol>/update")
def update(symbol):
    """改下载开关 / 起始日期"""
    try:
        payload = {"download": request.form.get("download") == "on"}
        if request.form.get("data_start"):
            payload["data_start"] = request.form["data_start"].strip()
        api.post_patch(f"/symbols/{symbol}", payload)
        flash(f"{symbol} 已更新", "ok")
    except api.ApiError as e:
        flash(f"更新失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/<symbol>/reverify")
def reverify(symbol):
    """重新向券商校验并刷新精度 (等价于重新登记同名品种)"""
    try:
        result = api.post("/symbols", {"symbol": symbol})
        flash(f"{result['symbol']} 已重新校验 (digits={result['digits']}, point={result['point']})", "ok")
    except api.ApiError as e:
        flash(f"校验失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/<symbol>/purge")
def purge(symbol):
    """清空该品种全部历史 K线 (删登记前的必经步骤, 也用于清孤儿)"""
    try:
        result = api.delete(f"/symbols/{symbol}/data")
        flash(f"{symbol} 已清空 {result['deleted_bars']:,} 根历史数据", "ok")
    except api.ApiError as e:
        flash(f"清空失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/<symbol>/delete")
def delete(symbol):
    """删除登记 (api 侧: 有数据会拒绝, 需先清空)"""
    try:
        api.delete(f"/symbols/{symbol}")
        flash(f"{symbol} 已删除", "ok")
    except api.ApiError as e:
        flash(f"删除失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))
