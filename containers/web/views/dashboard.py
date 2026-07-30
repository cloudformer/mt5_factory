"""概览页: 纯状态展示 (服务/数据库/worker/任务), 无操作"""
from flask import Blueprint, flash, render_template

import api_client as api

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    data = {"health": None, "hosts": [], "sync": {}, "backtest": {}, "coverage": []}
    try:
        data["health"] = api.get("/health")
        data["hosts"] = api.get("/hosts")["hosts"]
        data["sync"] = api.get("/syncdata/status")
        data["backtest"] = api.get("/backtest/status")
        # 覆盖懒加载(2026-07-30 Frank 定): 覆盖统计(大表 GROUP BY)慢 → 首页秒开, 覆盖卡片/表
        # 由页面 AJAX 走 /datasync/coverage 异步填(见 dashboard.html)
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("dashboard.html", **data)
