"""Demo / 实盘页: 指派执行主机 (demo 与 live 互斥) + 该状态策略列表"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("execution", __name__)

MODES = {
    "demo": {"role": "demo", "status": "DEMO", "title": "Demo 模拟"},
    "live": {"role": "live", "status": "ACTIVE", "title": "实时交易"},
}


def _render(mode: str):
    cfg = MODES[mode]
    hosts, strategies = [], []
    try:
        hosts = api.get("/hosts")["hosts"]
        strategies = api.get("/strategies/status", status=cfg["status"], limit=200)["strategies"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    assigned = [h for h in hosts if h["runner"] == cfg["role"]]
    assignable = [h for h in hosts if h["runner"] != cfg["role"]]
    return render_template("execution.html", mode=mode, cfg=cfg, assigned=assigned,
                           assignable=assignable, strategies=strategies)


@bp.get("/demo/")
def demo():
    return _render("demo")


@bp.get("/live/")
def live():
    return _render("live")


@bp.post("/<mode>/assign")
def assign(mode: str):
    if mode not in MODES:
        return redirect(url_for("dashboard.index"))
    role = MODES[mode]["role"]
    try:
        host_id = int(request.form["host_id"])
        result = api.post_patch(f"/hosts/{host_id}", {"runner": role})
        flash(f"{result['name']} 已指派为 {role} 主机", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"指派失败: {e}", "error")
    return redirect(url_for(f"execution.{mode}"))


@bp.post("/<mode>/unassign/<int:host_id>")
def unassign(mode: str, host_id: int):
    if mode not in MODES:
        return redirect(url_for("dashboard.index"))
    role = MODES[mode]["role"]
    try:
        result = api.post_patch(f"/hosts/{host_id}", {"runner": None})
        flash(f"{result['name']} 已取消 {role} 职能", "ok")
    except api.ApiError as e:
        flash(f"取消失败: {e}", "error")
    return redirect(url_for(f"execution.{mode}"))
