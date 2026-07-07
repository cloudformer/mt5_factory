"""下载页: 品种/起始日期配置 + 触发同步 + 数据覆盖"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("datasync", __name__, url_prefix="/datasync")


@bp.get("/")
def index():
    data = {"coverage": [], "sync": {}, "config": {}, "hosts": []}
    try:
        data["coverage"] = api.get("/syncdata/coverage")["coverage"]
        data["sync"] = api.get("/syncdata/status")
        data["config"] = api.get("/config")["config"]
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


@bp.post("/settings")
def settings():
    try:
        symbols = [s.strip().upper() for s in request.form["symbols"].split(",") if s.strip()]
        api.put("/config/symbols", {"value": symbols})
        api.put("/config/data_start", {"value": request.form["data_start"].strip()})
        flash("下载设置已保存", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("datasync.index"))
