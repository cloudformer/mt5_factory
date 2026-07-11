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
        # 数据覆盖来自品种主档 (symbols 表随附每品种 M1 覆盖)
        data["coverage"] = [s for s in api.get("/symbols")["symbols"] if s.get("bars")]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("dashboard.html", **data)
