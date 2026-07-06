"""概览页: worker 管理 / 下载设置 / 数据覆盖 / 同步与回测进度"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("dashboard", __name__)

ALL_ROLES = ["download", "backtest", "live"]


@bp.get("/")
def index():
    data = {"hosts": [], "coverage": [], "sync": {}, "backtest": {}, "config": {}}
    try:
        data["hosts"] = api.get("/hosts")["hosts"]
        data["coverage"] = api.get("/syncdata/coverage")["coverage"]
        data["sync"] = api.get("/syncdata/status")
        data["backtest"] = api.get("/backtest/status")
        data["config"] = api.get("/config")["config"]
    except api.ApiError as e:
        flash(f"app API 不可用: {e}", "error")
    return render_template("dashboard.html", all_roles=ALL_ROLES, **data)


@bp.post("/actions/sync")
def trigger_sync():
    try:
        api.post("/syncdata")
        flash("同步已启动", "ok")
    except api.ApiError as e:
        flash(f"启动同步失败: {e}", "error")
    return redirect(url_for("dashboard.index"))


# ---------- worker 管理 ----------
@bp.post("/workers/add")
def add_worker():
    try:
        result = api.post("/hosts", {
            "name": request.form["name"].strip(),
            "host": request.form["host"].strip(),
            "port": request.form.get("port", 9090, type=int),
            "roles": request.form.getlist("roles"),
            "account_type": request.form.get("account_type", "DEMO"),
        })
        flash(f"worker {result['name']} 已注册 (id={result['id']})", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"注册失败: {e}", "error")
    return redirect(url_for("dashboard.index"))


@bp.post("/workers/<int:host_id>/toggle")
def toggle_worker(host_id: int):
    try:
        enabled = request.form["enabled"] == "true"
        result = api.post_patch(f"/hosts/{host_id}", {"enabled": enabled})
        flash(f"{result['name']} 已{'启用' if enabled else '停用'}", "ok")
    except api.ApiError as e:
        flash(f"操作失败: {e}", "error")
    return redirect(url_for("dashboard.index"))


@bp.post("/workers/<int:host_id>/connect")
def connect_worker(host_id: int):
    try:
        result = api.post(f"/hosts/{host_id}/connect", {
            "login": int(request.form["login"]),
            "password": request.form["password"],
            "server": request.form["server"].strip(),
        })
        flash(f"MT5 已登录: {result.get('login')} @ {result.get('server')}", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"下发账户失败: {e}", "error")
    return redirect(url_for("dashboard.index"))


# ---------- 下载设置 ----------
@bp.post("/settings/save")
def save_settings():
    try:
        symbols = [s.strip().upper() for s in request.form["symbols"].split(",") if s.strip()]
        api.put("/config/symbols", {"value": symbols})
        api.put("/config/data_start", {"value": request.form["data_start"].strip()})
        flash("下载设置已保存", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("dashboard.index"))
