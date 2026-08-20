"""预测验证(v0.7 批次3) — 验证"门(regime)能否预测未来策略表现"。

口径(2026-08-08 与 Frank 定, docs/2.regime_dirction/v0.7):
  · 冻结日 = 带门策略的生成日期(created_at, 天然存在不可篡改) — 防泄露的本质:
    Expected 只用冻结日之前的数据, Actual 只用之后的, 同一份历史绝不既筛又自证
  · Expected = 冻结日前 expected_window_years(默认3年)的门内战绩, 首次算出即落库
    (UNIQUE + ON CONFLICT DO NOTHING = 永不覆盖), 之后引擎/参数怎么变都不漂移
  · Actual 读时现拼(冻结日 → 数据末端), 不落库
  · 读数两个 + 门槛一个(全配置, 不做合成分):
      保持率 = Actual PF / Expected PF        — 优势随时间衰减了多少
      同期增益 = 门内 Actual PF / 同期无门(父实例)PF — 门的真实预测价值
      成熟度 = 笔数≥min_trades 且 天数≥min_days 才出结论, 之前 = 未证实
  · 结论三档: 有效(Actual>1 且保持率≥retention_ok) / 衰减(Actual>1 但不足) / 失效(Actual≤1)
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from src.services import backtest, oos_v2

logger = logging.getLogger("prediction")


def cfg_params(cfg: dict) -> dict:
    return {"expected_window_years": float(cfg.get("expected_window_years") or 3),
            "min_trades": int(cfg.get("min_trades") or 20),
            "min_days": int(cfg.get("min_days") or 90),
            "retention_ok": float(cfg.get("retention_ok") or 0.8),
            # 稳定性批大小(2026-08-10 Frank 定): 每 N 笔一批各算 PF, 批间差不多才叫稳
            "stability_batch": int(cfg.get("stability_batch") or 30)}


def batch_pfs(trades: list, t0: float, t1: float, batch: int) -> dict | None:
    """批次 PF 序列(2026-08-10 Frank 定): [t0,t1) 窗内逐笔按时间切成每 batch 笔一批,
    各批用引擎 _metrics 算 PF(尺子同一把) — 批间 PF 差不多才稳, 不许一笔大单拉开均值。
    末尾不足一批的丢弃; 一个整批都不够 = 返回 None(样本不足)。
    读数: pfs 逐批(None=该批无亏损=∞), min=数字批最低(短板), below1=亏损批数。"""
    win = sorted((t for t in trades if t0 <= t["entry_time"] < t1),
                 key=lambda t: t["entry_time"])
    chunks = [win[i:i + batch] for i in range(0, len(win) - batch + 1, batch)]
    if not chunks:
        return None
    pfs = [backtest._metrics(c).get("profit_factor") for c in chunks]
    num = [p for p in pfs if p is not None]
    return {"batch": batch, "n_batches": len(chunks), "n_trades": len(win),
            "pfs": [None if p is None else round(p, 2) for p in pfs],
            "min": round(min(num), 2) if num else None,    # None=全部批无亏损(∞)
            "below1": sum(1 for p in num if p <= 1)}


async def board(pool, batch: int = 30, gated_only: bool = True,
                limit: int = 50, offset: int = 0, ids: list | None = None,
                is_disconnected=None) -> dict:
    """策略预测看板(2026-08-10 Frank 定, 读时现拼零落库): 锚 = 创建时间 —
    过去 = 整个回测窗(多少年无所谓, 如实显示)按每 batch 笔一批的 PF 序列;
    之后 = 创建日 → 数据末端合并一个 PF(现在笔数少, 攒多了再分批)。
    batch 是页面控件传参(不落库); gated_only=False 时无门策略也进(锚语义相同)。

    服务端分页(2026-08-15 Frank 报卡死): scope=全部时策略数以千计, 每个都要拉全量
    trades JSONB 现算批次 PF — 一次全算 = api 算几分钟 + 响应几十 MB。
    与全站同规矩: 只算当前页, 名单查询本身很便宜, total 从它出。

    取消业务(2026-08-15 Frank 要, 起因 = api 单核 98% 卡死事故): 调用方断开(web 超时/
    刷新/关页)后继续算是纯浪费 — 逐策略问一次 is_disconnected, 断了立刻弃算。
    重计算(batch_pfs)扔线程池: 它原来跑在事件循环里, 算的时候整个 api(连 /health)都被
    拖住, 心跳会误判; to_thread 后别的请求照常响应, is_disconnected 也才问得及时。"""
    where = "metadata->'regime'->>'version' IS NOT NULL" if gated_only \
        else "EXISTS (SELECT 1 FROM backtests b WHERE b.strategy_id = strategies.id)"
    args = []
    if ids:                                   # 按 ID 点名(2026-08-15): 点名即无视范围过滤
        where, args = "id = ANY($1)", [ids]
    all_rows = await pool.fetch(
        f"SELECT id, name, symbol, broker, timeframe, status, metadata, created_at"
        f"  FROM strategies WHERE {where} ORDER BY created_at DESC", *args)
    total = len(all_rows)
    rows = all_rows[offset:offset + limit]
    now_ts = datetime.now(timezone.utc).timestamp()
    out = []
    for s in rows:
        if is_disconnected is not None and await is_disconnected():
            logger.info("prediction board 取消: 调用方已断开(算到 %d/%d)",
                        len(out), len(rows))
            break                      # 没人收结果了, 算完是纯浪费
        bt = await pool.fetchrow(
            "SELECT from_time, to_time, trades FROM backtests"
            " WHERE strategy_id=$1 AND symbol=$2 AND broker=$3",
            s["id"], s["symbol"], s["broker"])
        trades = (bt["trades"] if bt else None) or []
        frozen_ts = s["created_at"].timestamp()
        g = (s["metadata"] or {}).get("regime") or {}
        after = (await asyncio.to_thread(slice_metrics, trades, frozen_ts, now_ts)
                 if bt else None)
        out.append({
            "id": s["id"], "name": s["name"], "symbol": s["symbol"],
            "timeframe": s["timeframe"], "status": s["status"],
            "gate_version": g.get("version"), "gate_cells": g.get("cells") or {},
            "frozen_at": s["created_at"].isoformat(),
            "window": (f"{bt['from_time']:%Y-%m-%d} ~ {bt['to_time']:%Y-%m-%d}"
                       if bt else None),     # 回测窗如实显示, 多少年无所谓
            "before": (await asyncio.to_thread(
                            batch_pfs, trades, bt["from_time"].timestamp(),
                            frozen_ts, batch)
                       if bt else None),
            "after": after,                  # {n, pf, net...} 合并一个数
        })
    return {"rows": out, "total": total}


def _pf_num(m: dict):
    """引擎指标 → 比值可用的 PF: 无亏损(None)按 ∞ 处理由调用方判, 0笔 None"""
    if not m["trades"]:
        return None
    return m.get("profit_factor")   # None(有笔无亏损) = ∞


def slice_metrics(trades: list, t0: float, t1: float) -> dict:
    """[t0, t1) 切片 → 引擎 _metrics(尺子同一把)"""
    m = backtest._metrics([t for t in trades if t0 <= t["entry_time"] < t1])
    return {"n": m["trades"], "net": m.get("net_points", 0.0),
            "pf": m.get("profit_factor"), "dd": m.get("max_dd_points"),
            "win_rate": m.get("win_rate")}


def judge(expected_pf, actual: dict, baseline_pf, days: int, p: dict) -> dict:
    """验证结论(纯函数, 测试锁死):
    成熟度不足 → 未证实(不打分); Actual≤1 → 失效; 保持率≥retention_ok → 有效; 否则衰减。
    PF=∞(无亏损)按恒过处理: expected ∞ → 保持率按 actual 自身>1 判; actual ∞ → 有效。"""
    n = actual["n"]
    if n < p["min_trades"] or days < p["min_days"]:
        return {"verdict": "immature",
                "reason": f"未证实: 已验证 {days} 天 / {n} 笔"
                          f"(门槛 {p['min_days']} 天 · {p['min_trades']} 笔)"}
    apf = actual["pf"]
    if apf is None and n:            # 有笔无亏损 = ∞ → 恒有效
        return {"verdict": "valid", "retention": None, "gain": None,
                "reason": "有效: 验证段无亏损(PF=∞)"}
    if apf is None or apf <= 1:
        return {"verdict": "broken", "retention": None,
                "gain": round(apf / baseline_pf, 3) if apf and baseline_pf else None,
                "reason": f"失效: 验证段 PF {apf if apf is not None else '—'} ≤ 1"}
    retention = None if expected_pf in (None, 0) else round(apf / expected_pf, 3)
    gain = round(apf / baseline_pf, 3) if baseline_pf else None
    ok = retention is None or retention >= p["retention_ok"]   # expected ∞ → 看 actual 自身
    return {"verdict": "valid" if ok else "decayed",
            "retention": retention, "gain": gain,
            "reason": (f"{'有效' if ok else '衰减'}: 保持率 "
                       f"{retention if retention is not None else '—(期望∞)'}"
                       + (f" · 同期增益 {gain}" if gain is not None else ""))}


async def ensure_snapshot(pool, strat: dict) -> dict | None:
    """带门策略的预测快照: 有则读, 无则用【冻结日之前】的门内战绩现算并落库(此后永不覆盖)。
    None = 无门策略 / 还没有回测行(下次再试)。strat 需含 id/symbol/metadata/created_at。"""
    g = (strat.get("metadata") or {}).get("regime") \
        if isinstance(strat.get("metadata"), dict) else None
    if not (isinstance(g, dict) and g.get("cells")):
        return None      # 无门 = 无预测对象
    row = await pool.fetchrow(
        "SELECT frozen_at, state_version, state_key, expected, created_at"
        " FROM predictions WHERE strategy_id=$1 ORDER BY frozen_at DESC LIMIT 1",
        strat["id"])
    if row:
        return dict(row)
    bt = await pool.fetchrow(
        "SELECT trades FROM backtests WHERE strategy_id=$1 AND symbol=$2"
        " AND broker=(SELECT broker FROM strategies WHERE id=$1)",
        strat["id"], strat["symbol"])
    if bt is None:
        return None      # 没回测行: 快照下次再冻(Expected 由冻结日决定, 晚算不漂移)
    p = cfg_params(await pool.fetchval(
        "SELECT value FROM config WHERE key='prediction'") or {})
    frozen = strat["created_at"]
    t0 = (frozen - timedelta(days=p["expected_window_years"] * oos_v2.YEAR_DAYS)).timestamp()
    exp = slice_metrics(bt["trades"] or [], t0, frozen.timestamp())
    exp["window_years"] = p["expected_window_years"]
    state_key = "·".join(sorted(g["cells"]))
    await pool.execute(
        "INSERT INTO predictions (strategy_id, frozen_at, state_version, state_key, expected)"
        " VALUES ($1, $2, $3, $4, $5) ON CONFLICT (strategy_id, frozen_at) DO NOTHING",
        strat["id"], frozen, int(g["version"]), state_key, exp)
    logger.info("prediction frozen: #%s %s v%s expected_pf=%s",
                strat["id"], state_key, g["version"], exp.get("pf"))
    return await pool.fetchrow(
        "SELECT frozen_at, state_version, state_key, expected, created_at"
        " FROM predictions WHERE strategy_id=$1 ORDER BY frozen_at DESC LIMIT 1",
        strat["id"])


async def validate(pool, strat: dict) -> dict | None:
    """预测验证读数(读时现拼): 快照 Expected(冻结) vs Actual(冻结日之后, 门内回测重放)
    vs Baseline(同期无门 = 父实例同段, 增益分母)。None = 无门/无快照可冻。"""
    snap = await ensure_snapshot(pool, strat)
    if snap is None:
        return None
    p = cfg_params(await pool.fetchval(
        "SELECT value FROM config WHERE key='prediction'") or {})
    frozen = snap["frozen_at"]
    now_ts = datetime.now(timezone.utc).timestamp()
    bt = await pool.fetchrow(
        "SELECT to_time, trades FROM backtests WHERE strategy_id=$1 AND symbol=$2"
        " AND broker=(SELECT broker FROM strategies WHERE id=$1)",
        strat["id"], strat["symbol"])
    actual = slice_metrics(bt["trades"] or [], frozen.timestamp(), now_ts) if bt \
        else {"n": 0, "net": 0.0, "pf": None, "dd": None, "win_rate": None}
    days = max(int((min(now_ts, bt["to_time"].timestamp() if bt else now_ts)
                    - frozen.timestamp()) // 86400), 0)
    # 同期基线 = 父实例(无门, 同参数)同段 — 门的增益分母; 没有父/父无行 = None
    baseline_pf = None
    if strat.get("parent_id"):
        pbt = await pool.fetchrow(
            "SELECT trades FROM backtests b JOIN strategies s ON s.id = b.strategy_id"
            "  AND b.symbol = s.symbol AND b.broker = s.broker"
            " WHERE b.strategy_id=$1", strat["parent_id"])
        if pbt:
            base = slice_metrics(pbt["trades"] or [], frozen.timestamp(), now_ts)
            baseline_pf = base["pf"] if base["n"] else None
    exp = snap["expected"]
    return {"report": f"prediction#{strat['id']}",
            "frozen_at": frozen.isoformat(), "state_version": snap["state_version"],
            "state_key": snap["state_key"], "expected": exp, "actual": actual,
            "baseline_pf": baseline_pf, "days_validated": days,
            **judge(exp.get("pf"), actual, baseline_pf, days, p)}
