"""下载页: 品种主档(唯一数据源)管理 + 触发同步

品种一切信息只在 symbols 表: 登记(向券商校验)、下载开关、每品种起始日期、精度。
下载/回测/策略生成都从这里读。本页只是它的管理界面。
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("datasync", __name__, url_prefix="/datasync")


@bp.get("/")
def index():
    data = {"symbols": [], "sync": {}, "hosts": []}
    try:
        data["symbols"] = api.get("/symbols")["symbols"]
        data["sync"] = api.get("/syncdata/status")
        data["hosts"] = [h for h in api.get("/hosts")["hosts"]
                         if h["enabled"] and h["download"]]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("datasync.html", **data)


@bp.post("/run")
def run():
    try:
        api.post("/syncdata")
        flash("同步已启动", "ok")
    except api.ApiError as e:
        flash(f"启动同步失败: {e}", "error")
    return redirect(url_for("datasync.index"))


@bp.post("/symbols/add")
def add_symbol():
    """登记品种: api 会向券商校验存在性并自动取真实精度"""
    try:
        result = api.post("/symbols", {
            "symbol": request.form["symbol"].strip().upper(),
            "role": request.form.get("role", "trade"),
            "data_start": request.form.get("data_start", "2015-01-01").strip(),
        })
        flash(f"{result['symbol']} 已登记 (digits={result['digits']}, point={result['point']})", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"登记失败: {e}", "error")
    return redirect(url_for("datasync.index"))


@bp.post("/symbols/<symbol>/update")
def update_symbol(symbol):
    """改下载开关 / 起始日期"""
    try:
        payload = {"download": request.form.get("download") == "on"}
        if request.form.get("data_start"):
            payload["data_start"] = request.form["data_start"].strip()
        api.post_patch(f"/symbols/{symbol}", payload)
        flash(f"{symbol} 已更新", "ok")
    except api.ApiError as e:
        flash(f"更新失败: {e}", "error")
    return redirect(url_for("datasync.index"))


@bp.post("/symbols/<symbol>/delete")
def delete_symbol(symbol):
    try:
        api.delete(f"/symbols/{symbol}")
        flash(f"{symbol} 已删除 (已下载数据保留)", "ok")
    except api.ApiError as e:
        flash(f"删除失败: {e}", "error")
    return redirect(url_for("datasync.index"))
