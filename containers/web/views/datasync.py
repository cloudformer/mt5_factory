"""下载页: 触发历史数据同步 + 每品种覆盖情况(只读)

品种在『品种』页维护(唯一数据源 symbols 表); 本页只负责把数据拉下来。
"""
from flask import Blueprint, flash, redirect, render_template, url_for

import api_client as api

bp = Blueprint("datasync", __name__, url_prefix="/datasync")


@bp.get("/")
def index():
    data = {"symbols": [], "orphans": [], "sync": {}, "hosts": []}
    try:
        s = api.get("/symbols")
        data["symbols"], data["orphans"] = s["symbols"], s.get("orphans", [])
        data["sync"] = api.get("/syncdata/status")
        data["hosts"] = [h for h in api.get("/hosts")["hosts"]
                         if h["enabled"] and h["download"]]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("datasync.html", **data)


@bp.get("/status")
def status():
    """同步进度 JSON — 供页面轮询更新进度条(与回测页同一模式)"""
    try:
        return api.get("/syncdata/status")
    except api.ApiError as e:
        return {"running": False, "error": str(e)}


@bp.post("/run")
def run():
    try:
        api.post("/syncdata")
        flash("同步已启动", "ok")
    except api.ApiError as e:
        flash(f"启动同步失败: {e}", "error")
    return redirect(url_for("datasync.index"))
