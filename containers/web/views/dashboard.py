"""概览组: 系统概览(服务/数据库/worker/任务) + 行情概览(当日八格落位+worker余额), 纯展示无操作"""
from datetime import datetime

from flask import Blueprint, flash, render_template

import api_client as api

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    data = {"health": None, "hosts": [], "sync": {}, "backtest": {}, "coverage": [],
            "pool": {}}
    try:
        data["health"] = api.get("/health")
        data["hosts"] = api.get("/hosts")["hosts"]
        data["sync"] = api.get("/syncdata/status")
        data["backtest"] = api.get("/backtest/status")
        data["pool"] = api.get("/overview/jobs")   # 跑批副本忙闲 + 队列待跑(任务卡)
        # 覆盖懒加载(2026-07-30 Frank 定): 覆盖统计(大表 GROUP BY)慢 → 首页秒开, 覆盖卡片/表
        # 由页面 AJAX 走 /datasync/coverage 异步填(见 dashboard.html)
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("dashboard.html", **data)


@bp.get("/overview/market")
def market_overview():
    """行情概览(2026-08-03 Frank 定样版): 当日八格落位(左上角=绝对数据日, 券商时间;
    落后品种灰显带截至日期) + worker 余额卡(悬停=初始资金/盈亏比例, 无出入金推算)"""
    data = None
    try:
        data = api.get("/overview/market")
        for v in (data or {}).get("versions", []):   # 每版本各自标记落后品种
            for lst in v["cells"].values():
                for e in lst:
                    e["stale"] = e["date"] != v["date"]
                    e["date_short"] = e["date"][5:10]   # MM-DD(截至标注用)
        for w in (data or {}).get("workers", []):
            if w.get("heartbeat"):
                w["hb_fmt"] = datetime.fromisoformat(w["heartbeat"]).strftime("%H:%M:%S")
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("market_overview.html", data=data)
