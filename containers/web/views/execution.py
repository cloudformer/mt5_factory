"""Demo / Live 页: 指派执行主机 (demo 与 live 互斥) + 该状态策略列表 + 账户/战绩展示

账户与每策略战绩来自 worker 回传链路: runner 落盘 → bridge /health → api 心跳存 last_health。
web 只读 api, 不直接连 worker。数据最多滞后一个心跳周期(30s)。
"""
from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("execution", __name__)

MODES = {
    "demo": {"role": "demo", "status": "DEMO", "title": "Demo"},
    "live": {"role": "live", "status": "LIVE", "title": "Live"},
}


def _runner_report(hosts: list) -> tuple:
    """从已指派主机的 last_health 提取 (账户列表, 按magic的战绩表, 按策略id的跳过表, 过期主机)。
    skipped: runner 加载时因"品种不在 MT5 报价窗"而跳过的策略 — 必须提示, 否则永远默默等。
    quote_stale: 品种在报价窗但最新 tick 已停滞(休市或断流), 同样提示。
    stale: 回传数据超过 3 分钟没更新 — 必须显式警告, 过期数据看起来和实时的一样, 会骗人。"""
    accounts, stats_by_magic, skipped_by_id, stale = [], {}, {}, []
    now = datetime.now(tz=timezone.utc).timestamp()
    for h in hosts:
        runner = ((h.get("last_health") or {}).get("runner")) or {}
        updated = runner.get("updated")
        if updated and now - updated > 180:
            stale.append({"name": h["name"], "minutes": int((now - updated) / 60)})
        account = runner.get("account")
        if account:
            accounts.append({"host": h["name"], **account})
        for s in runner.get("per_strategy") or []:
            if s.get("last_bar"):  # epoch → 可读时间 (bar 时间戳为券商服务器时间)
                s["last_bar_fmt"] = datetime.fromtimestamp(
                    s["last_bar"], tz=timezone.utc).strftime("%m-%d %H:%M")
            # 报价停滞: 最新 tick 距今超过 10 分钟 (bar 时间是券商时间, 与UTC偏差以小时计,
            # 10分钟阈值下休市/断流都会触发, 提示里两种原因并列)
            s["quote_stale"] = bool(s.get("quote_ts")) and (now - s["quote_ts"]) > 600
            stats_by_magic[s["magic"]] = s
        for sk in runner.get("skipped") or []:
            skipped_by_id[sk["id"]] = sk
    return accounts, stats_by_magic, skipped_by_id, stale


def _render(mode: str):
    cfg = MODES[mode]
    hosts, strategies, volume_presets, volume_default = [], [], [], None
    try:
        hosts = api.get("/hosts")["hosts"]
        strategies = api.get("/strategies/status", status=cfg["status"], limit=200)["strategies"]
        # 手数预设/默认(唯一源=config表): 本页手数下拉与策略列表同款
        c = api.get("/config")["config"]
        volume_presets = c.get("volume_presets") or []
        volume_default = c.get("volume_default")
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    assigned = [h for h in hosts if h["runner"] == cfg["role"]]
    # 只有"无职能"的主机可被指派; 已是 demo/live 的必须先取消指派 (api 侧也强制)
    assignable = [h for h in hosts if not h["runner"] and h["enabled"]]
    accounts, stats_by_magic, skipped_by_id, stale = _runner_report(assigned)
    return render_template("execution.html", mode=mode, cfg=cfg, assigned=assigned,
                           assignable=assignable, strategies=strategies,
                           accounts=accounts, stats=stats_by_magic, skipped=skipped_by_id,
                           stale=stale, volume_presets=volume_presets,
                           volume_default=volume_default)


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
