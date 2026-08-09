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


@bp.get("/overview/backtest")
def backtest_overview():
    """全局回测概览(2026-08-09 页面整编): 三卡(全量口径) + 原「对账统计」页明细整块并入。
    三卡算法唯一在 api(_recon_cards), 明细行 = /reconcile/summary(纯读已存结果)。"""
    cards, err = None, None
    rows, recon_hours, recon_last = [], 24, None
    try:
        summary = api.get("/reconcile/summary")
        cards = summary["cards"]
        rows = summary["strategies"]
        cfg = api.get("/config")["config"]
        recon_hours = int(cfg.get("recon_hours") or 0)   # 自动对账频率(小时, 0=关)
        recon_last = cfg.get("recon_last_run")
    except (api.ApiError, TypeError, ValueError) as e:
        err = str(e)
        flash(f"api 不可用: {e}", "error")
    return render_template("backtest_overview.html", cards=cards, err=err, rows=rows,
                           recon_hours=recon_hours, recon_last=recon_last)


@bp.get("/overview/worker-balance")
def worker_balance():
    """全局Worker余额(2026-08-09 从回测概览拆出): 每台在跑 worker 的余额/已实现 — 透传展示"""
    workers = []
    try:
        workers = (api.get("/overview/market") or {}).get("workers") or []
        for w in workers:
            if w.get("heartbeat"):
                w["hb_fmt"] = datetime.fromisoformat(w["heartbeat"]).strftime("%H:%M:%S")
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("worker_balance.html", workers=workers)


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
            # 八格显示行(2026-08-06 改用全站标准组件 m.regime_grid, 不再手写 table —
            # 手写表的底色/圆角与其他页八格不一致)。python 组好 HTML 行, 模板零逻辑
            v["lines"] = {}
            for cell in ("AAA", "AAB", "ABA", "ABB", "BAA", "BAB", "BBA", "BBB"):
                members = v["cells"].get(cell) or []
                if not members:
                    v["lines"][cell] = ['<span class="muted">—</span>']
                    continue
                v["lines"][cell] = [
                    f'<a class="mono{" muted" if e["stale"] else ""}"'
                    f' href="/datasync/regime?symbol={e["symbol"]}"'
                    f' title="已在 {cell} 连续 {e["run_days"]} 天'
                    + (f' · 数据截至 {e["date"]}' if e["stale"] else '') + '">'
                    + e["symbol"] + '</a>'
                    + (f' <span class="muted">(截至 {e["date_short"]})</span>'
                       if e["stale"] else '')
                    for e in members]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("market_overview.html", data=data)
