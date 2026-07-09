"""Demo / Live 页: 指派执行主机 (demo 与 live 互斥) + 该状态策略列表 + 账户/战绩展示

账户与每策略战绩来自 worker 回传链路: runner 落盘 → bridge /health → api 心跳存 last_health。
web 只读 api, 不直接连 worker。数据最多滞后一个心跳周期(30s)。
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("execution", __name__)

MODES = {
    "demo": {"role": "demo", "status": "DEMO", "title": "Demo"},
    "live": {"role": "live", "status": "LIVE", "title": "Live"},
}


def _runner_report(hosts: list) -> tuple:
    """从已指派主机的 last_health 提取 (账户列表, 按magic的策略战绩表)。
    账户按主机各一份(通常一台); 战绩合并所有主机(magic 全局唯一, 不冲突)。"""
    accounts, stats_by_magic = [], {}
    for h in hosts:
        runner = ((h.get("last_health") or {}).get("runner")) or {}
        account = runner.get("account")
        if account:
            accounts.append({"host": h["name"], **account})
        for s in runner.get("per_strategy") or []:
            stats_by_magic[s["magic"]] = s
    return accounts, stats_by_magic


def _render(mode: str):
    cfg = MODES[mode]
    hosts, strategies = [], []
    try:
        hosts = api.get("/hosts")["hosts"]
        strategies = api.get("/strategies/status", status=cfg["status"], limit=200)["strategies"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    assigned = [h for h in hosts if h["runner"] == cfg["role"]]
    # 只有"无职能"的主机可被指派; 已是 demo/live 的必须先取消指派 (api 侧也强制)
    assignable = [h for h in hosts if not h["runner"] and h["enabled"]]
    accounts, stats_by_magic = _runner_report(assigned)
    return render_template("execution.html", mode=mode, cfg=cfg, assigned=assigned,
                           assignable=assignable, strategies=strategies,
                           accounts=accounts, stats=stats_by_magic)


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
