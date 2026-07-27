"""Workers 页: worker 注册 / 启停 / 删除 / 下发 MT5 账户"""
import time
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("workers", __name__, url_prefix="/workers")

WATCH_MS = 120_000  # 指派/重启后自动刷新页面的时长(毫秒), 到点自停


def _watch():
    """返回带"观察截止时间戳"的 index URL — 页面据此自动刷新到点自停(见模板 JS)"""
    return url_for("workers.index", watch=int(time.time() * 1000) + WATCH_MS)


@bp.get("/")
def index():
    hosts = []
    try:
        hosts = api.get("/hosts")["hosts"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    for h in hosts:  # 自检时间戳(epoch)转可读, 模板直接用
        st = (h.get("last_health") or {}).get("selftest")
        if st and st.get("updated"):
            st["updated_fmt"] = datetime.fromtimestamp(st["updated"]).strftime("%m-%d %H:%M")
    return render_template("workers.html", hosts=hosts)


@bp.post("/assign")
def assign():
    """给已自动上报的 worker 指派运行状态(空闲/demo/live)。
    机器从下拉选(名字=真实计算机名, 不手输); worker 本身靠 bridge 自动注册, 无需手动加。"""
    try:
        host_id = int(request.form["host_id"])
        runner = request.form.get("runner") or None
        result = api.post_patch(f"/hosts/{host_id}", {"runner": runner})
        flash(f"{result['name']} → {result['runner'] or '空闲'}"
              " (角色/策略数约 15 秒后随 runner 心跳更新, 页面已自动刷新)", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"指派失败: {e}", "error")
    return redirect(_watch())


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


# 远程重启/下发账户路由已删(2026-07-26, v7.2 单向化取舍): api 不再主动连 worker —
# 重启上机操作(bridge 本地看门狗保留), 账户在部署时写机器 env(announce 自动回报)。
