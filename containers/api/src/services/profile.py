"""策略 Profile(v0.7 批次2) — 完整画像, 读时现拼不落库。

原则(2026-08-08 与 Frank 定): 一切决策都是读数 — Profile 的每一项原料已在库里
(策略行/tags履历/backtests/oos_v2报告/regime时间线/strategy_stats/对账),
现拼一个 dict 返回, 不造第二份要同步维护的副本。
与 AI 成绩单(/strategies/{id}/report, 喂AI用全量逐笔)分工: Profile 是【结论级画像】
(数字小而全, 人看/页面渲染/未来预测模块引用), 成绩单是【原始档案】。

刀法与 oos_v2 完全同源(anchor_dt/seg_window/_metrics 同一份函数) —
stability 三窗数字与 oos_v2 报告可逐字段对账。
"""
from datetime import datetime, timezone

from src.services import backtest, oos_v2, prediction, regime


def _win_stat(trades: list, a, years: float) -> dict:
    """近 N 年窗读数: 与 oos_v2 同刀法(锚点 UTC 0点/365.25天/左闭右开), 引擎 _metrics 同尺"""
    t0, t1 = oos_v2.seg_window(a, [years, 0])
    m = backtest._metrics([t for t in trades if t0 <= t["entry_time"] < t1])
    return {"years": years, "n": m["trades"], "net": m.get("net_points", 0.0),
            "pf": m.get("profit_factor"), "dd": m.get("max_dd_points"),
            "win_rate": m.get("win_rate")}


async def build(pool, strategy_id: int) -> dict | None:
    """现拼 Profile。None = 策略不存在。"""
    s = await pool.fetchrow(
        "SELECT id, name, template, symbol, timeframe, params, status, magic_number,"
        "       basis, tags, archive_reason, parent_id, origin, owner_id, volume,"
        "       metadata, created_at, updated_at"
        " FROM strategies WHERE id=$1", strategy_id)
    if s is None:
        return None
    anchor = datetime.now(timezone.utc).date()
    a = oos_v2.anchor_dt(anchor)

    # ---- stability: 主品种回测行按 20/5/2 年窗现切(窗口是读数口径, 数据够多少算多少) ----
    bt = await pool.fetchrow(
        "SELECT from_time, to_time, created_at, trades FROM backtests"
        " WHERE strategy_id=$1 AND symbol=$2", strategy_id, s["symbol"])
    stability = None
    if bt:
        trades = bt["trades"] or []
        stability = {
            "windows": [_win_stat(trades, a, y) for y in (20, 5, 2)],
            "coverage": f"{bt['from_time']:%Y-%m-%d} ~ {bt['to_time']:%Y-%m-%d}",
            "backtest_at": bt["created_at"].isoformat()}

    # ---- oos: 最近一次 oos_v2 报告里本策略那行(六段结论, 报告号可跳转) ----
    oos_row = await pool.fetchrow(
        "SELECT r.id AS report_id, r.anchor, r.created_at, e AS item"
        "  FROM oos_v2_screens r, jsonb_array_elements(r.details) e"
        " WHERE (e->>'id')::int = $1 ORDER BY r.id DESC LIMIT 1", strategy_id)
    oos = None
    if oos_row:
        it = oos_row["item"]
        oos = {"report": f"oos_v2#{oos_row['report_id']}",
               "anchor": oos_row["anchor"].isoformat(),
               "verdict": it.get("verdict"), "reason": it.get("reason"),
               "warn": it.get("warn"),
               "periods": [{"label": p.get("label"), "min_pf": p.get("min_pf"),
                            "train": {k: p["train"].get(k) for k in ("n", "net", "pf", "inf")},
                            "test": {k: p["test"].get(k) for k in ("n", "net", "pf", "inf")}}
                           for p in (it.get("periods") or [])]}

    # ---- states: 逐笔贴当日格(当前默认口径版本), 每格 pf/笔数/胜率 — 与九币矩阵同口径 ----
    states = None
    if bt:
        vid, _ = await regime.active_version(pool)
        tl = await regime.tl_map(pool, s["symbol"], vid)
        per_cell: dict = {}
        for t in (bt["trades"] or []):
            cell = tl.get(datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).date())
            if cell is None:
                continue
            acc = per_cell.setdefault(cell, [0, 0, 0.0, 0.0])  # n/wins/毛利/毛损
            pts = float(t.get("points") or 0) * float(t.get("mult") or 1)
            acc[0] += 1
            if pts > 0:
                acc[1] += 1
                acc[2] += pts
            else:
                acc[3] -= pts
        states = {"version": vid,
                  "cells": {c: {"n": v[0],
                                "win_rate": round(v[1] / v[0], 4) if v[0] else None,
                                "net": round(v[2] - v[3], 1),
                                "pf": round(v[2] / v[3], 3) if v[3] > 0
                                      else (None if v[2] > 0 else 0)}
                            for c, v in sorted(per_cell.items())}}

    # ---- live: demo/live 战绩(按 env 聚合过账户) + 最近对账可信度 ----
    envs = await pool.fetch(
        "SELECT env, sum(trades)::int AS trades, sum(wins)::int AS wins,"
        "       round(sum(profit)::numeric, 2)::float AS profit"
        "  FROM strategy_stats WHERE strategy_id=$1 GROUP BY env", strategy_id)
    recon = await pool.fetchrow(
        "SELECT scope, actual_trades, bt_trades, match_score, updated_at"
        "  FROM reconciliations WHERE strategy_id=$1"
        " ORDER BY updated_at DESC LIMIT 1", strategy_id)

    return {
        "base": {"id": s["id"], "name": s["name"], "template": s["template"],
                 "symbol": s["symbol"], "timeframe": s["timeframe"],
                 "params": s["params"], "status": s["status"],
                 "magic_number": s["magic_number"], "volume": s["volume"],
                 "origin": s["origin"], "parent_id": s["parent_id"],
                 "owner_id": s["owner_id"], "gate": s["metadata"] or None,
                 "created_time": s["created_at"].isoformat()},
        "history": {"tags": s["tags"] or [], "basis": s["basis"],
                    "archive_reason": s["archive_reason"]},
        "stability": stability,          # None = 还没有回测行
        "oos": oos,                      # None = 没进过 oos_v2 报告
        "states": states,                # None = 没有回测行
        "live": {"envs": {r["env"]: {"trades": r["trades"], "wins": r["wins"],
                                     "profit": r["profit"]} for r in envs},
                 "reconcile": ({"scope": recon["scope"],
                                "actual_trades": recon["actual_trades"],
                                "bt_trades": recon["bt_trades"],
                                "match_score": recon["match_score"],
                                "at": recon["updated_at"].isoformat()} if recon else None)},
        "prediction": await prediction.validate(pool, dict(s)),
                                         # None=无门; 带门=快照冻结值+验证读数(批次3)
        "profile_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "anchor": anchor.isoformat(),
    }
