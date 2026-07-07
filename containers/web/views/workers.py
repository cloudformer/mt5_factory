"""Workers 页: worker 注册 / 启停 / 删除 / 下发 MT5 账户"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("workers", __name__, url_prefix="/workers")


@bp.get("/")
def index():
    hosts = []
    try:
        hosts = api.get("/hosts")["hosts"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("workers.html", hosts=hosts)


@bp.post("/add")
def add():
    try:
        runner = request.form.get("runner") or None
        result = api.post("/hosts", {
            "name": request.form["name"].strip(),
            "host": request.form["host"].strip(),
            "port": request.form.get("port", 8020, type=int),
            "download": request.form.get("download") == "on",
            "runner": runner,
            "account_type": request.form.get("account_type", "DEMO"),
        })
        flash(f"worker {result['name']} 已注册 (id={result['id']})", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"注册失败: {e}", "error")
    return redirect(url_for("workers.index"))


@bp.post("/<int:host_id>/toggle")
def toggle(host_id: int):
    try:
        enabled = request.form["enabled"] == "true"
        result = api.post_patch(f"/hosts/{host_id}", {"enabled": enabled})
        flash(f"{result['name']} 已{'启用' if enabled else '停用'}", "ok")
    except api.ApiError as e:
        flash(f"操作失败: {e}", "error")
    return redirect(url_for("workers.index"))


@bp.post("/<int:host_id>/delete")
def delete(host_id: int):
    try:
        result = api.delete(f"/hosts/{host_id}")
        flash(f"worker {result['deleted']} 已删除", "ok")
    except api.ApiError as e:
        flash(f"删除失败: {e}", "error")
    return redirect(url_for("workers.index"))


@bp.post("/<int:host_id>/connect")
def connect(host_id: int):
    try:
        result = api.post(f"/hosts/{host_id}/connect", {
            "login": int(request.form["login"]),
            "password": request.form["password"],
            "server": request.form["server"].strip(),
        })
        flash(f"MT5 已登录: {result.get('login')} @ {result.get('server')}", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"下发账户失败: {e}", "error")
    return redirect(url_for("workers.index"))
