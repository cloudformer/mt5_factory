"""品种页: 品种主档(唯一数据源)的独立维护界面

品种一切信息只在 symbols 表: 登记(向券商校验)、精度、下载开关、每品种起始日期。
下载/回测/策略生成都从这里读。本页是它唯一的管理入口。
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("symbols", __name__, url_prefix="/symbols")


@bp.get("/")
def index():
    """配置页: 货币对主档 + 回测参数(成本模型)"""
    symbols, orphans, costs = [], [], {}
    try:
        data = api.get("/symbols")
        symbols, orphans = data["symbols"], data.get("orphans", [])
        costs = api.get("/config")["config"].get("backtest_costs", {})
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("symbols.html", symbols=symbols, orphans=orphans, costs=costs)


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
