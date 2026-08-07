"""回测引擎: M1 回放 + 悲观撮合

撮合规则(CLAUDE.md 准入漏斗):
- 信号在 TF bar 收盘产生, 下一根 M1 开盘价成交
- BUY 以 ask 成交(bid + 当根点差 + 滑点), 以 bid 离场; SELL 反之
- SL/TP 用 M1 逐根检查; 同一根 M1 同时碰到 → 按止损算(悲观)
- 跳空穿价按实际开盘价成交, 不按挂单价
- 佣金按点数从每笔盈亏中扣除
"""
import logging
from datetime import datetime, timezone

import numpy as np

from strategy_core import TF_SECONDS, make_strategy
from strategy_core.gate import gate_mult
from strategy_core.trailing import atr_m1, trail_new_sl

logger = logging.getLogger("backtest")

# 成本模型默认值 (可被回测请求参数覆盖)
DEFAULT_SLIPPAGE_POINTS = 3.0     # 单边滑点
DEFAULT_COMMISSION_POINTS = 7.0   # 往返佣金(点数等值)


def _spread_at(m1, j, point, spread_points):
    """当根点差(价格单位): 指定 spread_points 则固定点差, 否则用 bar 记录的真实点差"""
    return (spread_points if spread_points is not None else m1["spread"][j]) * point


async def reuse_row(pool, strategy_id: int, symbol: str,
                    t_from: datetime, t_to: datetime):
    """回测复用守卫(2026-08-07 与 Frank 定, 全局唯一实现 — 所有走库的回测路径共用):
    config backtest_reuse_days(0=关) 天内跑过、且行跨度覆盖本次要求窗口(差45天容差,
    吸收数据首根晚几天/周末)的 (策略×品种) 行 → 返回该行(调用方直接用, 不重跑); 否则 None。
    大窗行永远能当小窗用(20年行喂饱 5年/180天请求); 小窗行冒充不了大请求。
    覆盖: jobs._run_one(批量/单ID/v1/oos_v2 队列) + 两筛选的点名诊断同步路径;
    不覆盖: trail 变体对比(内存现算, 参数是临时变体 — 复用即错误)。"""
    rd = int(await pool.fetchval(
        "SELECT value FROM config WHERE key='backtest_reuse_days'") or 0)
    if not rd:
        return None
    need_days = max(int((t_to - t_from).days) - 45, 1)
    return await pool.fetchrow(
        "SELECT from_time, to_time, metrics, trades, created_at FROM backtests"
        " WHERE strategy_id = $1 AND symbol = $2"
        "   AND created_at >= now() - make_interval(days => $3)"
        "   AND to_time - from_time >= make_interval(days => $4)",
        strategy_id, symbol, rd, need_days)


async def load_m1(pool, symbol: str, t_from: datetime, t_to: datetime):
    """从 historical_bars 加载 M1 到 numpy 数组"""
    where = "symbol=$1 AND timeframe='M1' AND time >= $2 AND time < $3"
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            f"SELECT count(*) FROM historical_bars WHERE {where}", symbol, t_from, t_to)
        if n == 0:
            return None
        arr = {
            "time": np.empty(n, np.int64),
            "open": np.empty(n), "high": np.empty(n),
            "low": np.empty(n), "close": np.empty(n),
            "spread": np.empty(n, np.int64),
        }
        i = 0
        async with conn.transaction():
            async for r in conn.cursor(
                f"SELECT extract(epoch FROM time)::bigint, open, high, low, close, spread"
                f"  FROM historical_bars WHERE {where} ORDER BY time",
                symbol, t_from, t_to,
            ):
                arr["time"][i], arr["open"][i], arr["high"][i] = r[0], r[1], r[2]
                arr["low"][i], arr["close"][i], arr["spread"][i] = r[3], r[4], r[5]
                i += 1
        return arr


def aggregate(m1: dict, tf_seconds: int) -> dict:
    """M1 → 高周期, 按时间桶聚合(缺分钟安全), 并记录每根TF bar对应的M1切片"""
    bucket = m1["time"] // tf_seconds
    change = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change, [len(bucket)]))
    return {
        "time": bucket[starts] * tf_seconds,
        "open": m1["open"][starts],
        "high": np.maximum.reduceat(m1["high"], starts),
        "low": np.minimum.reduceat(m1["low"], starts),
        "close": m1["close"][ends - 1],
        "m1_start": starts,
        "m1_end": ends,
    }


def _walk_exit(pos, j_from, j_to, m1, point, spread_points, trail=None):
    """M1 逐根检查 SL/TP。悲观: 先查SL后查TP; 跳空按开盘价。返回 (exit_price, j, reason) 或 None。
    trail(v0.9 移动止损): 每根 M1 收盘后棘轮更新 pos["sl"](只向有利方向), 生效于下一根 —
    与实盘 runner"看最新收盘 M1 改单"同一语义, 两边逐笔一致。"""
    o, h, l = m1["open"], m1["high"], m1["low"]
    for j in range(j_from, j_to):
        if pos["dir"] == "BUY":  # 以 bid 离场, bar本身就是bid价
            if o[j] <= pos["sl"]:
                return float(o[j]), j, "sl_gap"
            if l[j] <= pos["sl"]:
                return pos["sl"], j, "sl"
            if o[j] >= pos["tp"]:
                return float(o[j]), j, "tp_gap"
            if h[j] >= pos["tp"]:
                return pos["tp"], j, "tp"
        else:  # SELL 以 ask 离场, ask ≈ bid + 当根点差
            sp = _spread_at(m1, j, point, spread_points)
            if o[j] + sp >= pos["sl"]:
                return float(o[j] + sp), j, "sl_gap"
            if h[j] + sp >= pos["sl"]:
                return pos["sl"], j, "sl"
            if o[j] + sp <= pos["tp"]:
                return float(o[j] + sp), j, "tp_gap"
            if l[j] + sp <= pos["tp"]:
                return pos["tp"], j, "tp"
        if trail:  # 本根未出场 → 按收盘价棘轮 SL(参考价口径与撮合一致: BUY=bid收盘, SELL=ask收盘)
            ref = float(m1["close"][j]) + (
                _spread_at(m1, j, point, spread_points) if pos["dir"] == "SELL" else 0.0)
            atr = (atr_m1(m1, j, int((trail.get("atr") or {}).get("period") or 14))
                   if trail.get("active") == "atr" else None)
            ns = trail_new_sl(pos["dir"], pos["entry"], pos["sl"], ref, trail, point, atr)
            if ns is not None:
                pos["sl"] = ns
                pos["trailed"] = True
    return None


def run_backtest(m1: dict, template: str, params: dict, point: float, timeframe: str,
                 slippage_points: float = DEFAULT_SLIPPAGE_POINTS,
                 commission_points: float = DEFAULT_COMMISSION_POINTS,
                 spread_points: float | None = None,
                 oos_split: float | None = 0.7,
                 start_ts: int | None = None,
                 gate: dict | None = None) -> dict:
    """单个策略实例回测, 返回 {metrics, trades}

    成本模型参数:
    - slippage_points:   单边滑点(点), 进场时向不利方向偏移
    - commission_points: 往返佣金(点数等值), 每笔盈亏中扣除
    - spread_points:     固定点差(点); None=用每根bar记录的真实点差(默认, 推荐)
    - oos_split:         样本外切分比例(训练段占比, 默认0.7; None=不切) → metrics["oos"]
    - start_ts:          该时刻(epoch)之前只喂指标不开新仓 — 对账重放用, 复现"实盘空仓上线"
                         (None=从头可开仓, 与历史行为逐字节一致)
    - gate:              regime 门(v0.3): {"cells": {格:倍率}, "tl": {券商日期: 格}} —
                         入场日格不在 cells / 无格 → 不开新仓(占位真实释放=级联语义);
                         在 → 每笔带 mult, 指标按倍率加权(points 存原始价差, 对账可比)。
                         None=无门, 路径与历史逐字节一致(裁决共用 strategy_core.gate)
    """
    strat = make_strategy(template, params, point)
    tf = aggregate(m1, TF_SECONDS[timeframe])
    w = strat.warmup
    n = len(tf["time"])
    slip = slippage_points * point
    commission = commission_points
    # 移动止损(v0.9): 配置随策略 params 走(全局默认的回落在 api 层做, 引擎只认 params)
    trail = params.get("trail") if isinstance(params, dict) else None
    if not (isinstance(trail, dict) and trail.get("active")):
        trail = None

    pos = None
    trades = []
    for i in range(w, n - 1):
        j_from, j_to = int(tf["m1_start"][i + 1]), int(tf["m1_end"][i + 1])

        if pos is None:
            if start_ts is not None and m1["time"][j_from] < start_ts:
                continue  # 起点对齐: 入场bar早于起点 → 空仓略过(指标窗口照常前进)
            sig = strat.on_bar(
                tf["open"][i - w + 1:i + 1], tf["high"][i - w + 1:i + 1],
                tf["low"][i - w + 1:i + 1], tf["close"][i - w + 1:i + 1],
            )
            if sig is None:
                continue
            mult = 1.0
            if gate is not None:   # regime 门: 入场日的格子裁决(与 runner 共用 gate_mult)
                cell = gate["tl"].get(datetime.fromtimestamp(
                    int(m1["time"][j_from]), tz=timezone.utc).date())
                m_ = gate_mult(gate, cell)
                if m_ is None:
                    continue   # 门外/当日无格: 不开新仓(仓位真实空出 — 占位级联语义)
                mult = m_
            j = j_from
            if sig.direction == "BUY":  # 买在 ask + 滑点
                entry = float(m1["open"][j] + _spread_at(m1, j, point, spread_points) + slip)
            else:  # 卖在 bid - 滑点
                entry = float(m1["open"][j] - slip)
            pos = {"dir": sig.direction, "entry": entry, "sl": sig.sl, "tp": sig.tp,
                   "entry_time": int(m1["time"][j]), "mae": 0.0, "mfe": 0.0, "mult": mult}
            if trail and trail.get("keep_tp") is False:   # 去掉TP让利润跑, 只靠移动SL出场
                pos["tp"] = float("inf") if sig.direction == "BUY" else float("-inf")

        hit = _walk_exit(pos, j_from, j_to, m1, point, spread_points, trail=trail)
        # MAE/MFE(点): 持仓期间最大浮亏/浮盈游程 — AI 调 SL/TP 的直接依据(bid 价近似)。
        # 只扫到出场那根为止; M1 已在内存, min/max 零额外 IO
        seg_end = (hit[1] + 1) if hit else j_to
        if seg_end > j_from:
            lo = float(m1["low"][j_from:seg_end].min())
            hi = float(m1["high"][j_from:seg_end].max())
            if pos["dir"] == "BUY":
                pos["mae"] = max(pos["mae"], (pos["entry"] - lo) / point)
                pos["mfe"] = max(pos["mfe"], (hi - pos["entry"]) / point)
            else:
                pos["mae"] = max(pos["mae"], (hi - pos["entry"]) / point)
                pos["mfe"] = max(pos["mfe"], (pos["entry"] - lo) / point)
        if hit:
            exit_price, j, reason = hit
            # 被追过的 SL 触发 = 锁利出场, 单独记 tsl(开/关对比与归因区分"原始止损 vs 移动止损")
            if pos.get("trailed") and reason in ("sl", "sl_gap"):
                reason = {"sl": "tsl", "sl_gap": "tsl_gap"}[reason]
            sign = 1 if pos["dir"] == "BUY" else -1
            points = sign * (exit_price - pos["entry"]) / point - commission
            trades.append({
                "dir": pos["dir"], "entry_time": pos["entry_time"],
                "exit_time": int(m1["time"][j]), "entry": round(pos["entry"], 6),
                "exit": round(exit_price, 6), "points": round(points, 1), "reason": reason,
                "mae": round(pos["mae"], 1), "mfe": round(pos["mfe"], 1),
                # 开仓时的 SL(2026-07-29): 对账据此判"止损<券商最小停损距离→实盘会被拒"
                "sl": round(pos["sl"], 6) if pos.get("sl") is not None else None,
                # regime 门(v0.3): 带门实例记录该笔倍率(指标加权用); 无门不写键(载荷与历史一致)
                **({"mult": pos["mult"]} if gate is not None else {}),
            })
            pos = None

    metrics = _metrics(trades)
    metrics["settings"] = {  # 成本模型随结果存档, 不同设置的成绩不混淆
        "slippage_points": slippage_points,
        "commission_points": commission_points,
        "spread_points": spread_points if spread_points is not None else "recorded",
    }
    if oos_split and len(m1["time"]) > 1:
        # 样本外(OOS)切分 — 反过拟合时间维度(v1.3 #1)。纯后处理: 撮合零改动,
        # 按时间把已成交的 trades 切成 训练段(前 split) / 留出段(后 1-split) 各算一份。
        # 纪律: 训练段用来选, 留出段只用来一票否决(留出亏=过拟合嫌疑, 不准进 demo)。
        split_ts = int(m1["time"][0] + oos_split * (m1["time"][-1] - m1["time"][0]))
        train = [t for t in trades if t["entry_time"] < split_ts]
        metrics["oos"] = {
            "split": oos_split,
            "split_time": split_ts,
            "train": _metrics(train),
            "holdout": _metrics([t for t in trades if t["entry_time"] >= split_ts]),
        }
    return {"metrics": metrics, "trades": trades}


def _metrics(trades: list) -> dict:
    if not trades:
        return {"trades": 0, "net_points": 0.0}
    # regime 门(v0.3): 带门实例每笔有 mult(0.5~1), 指标按倍率加权 = 经济视角;
    # t["points"] 永远存原始价差(与实盘逐笔可比, 对账用) — 无门时 mult 缺省 1, 数值零变化
    pts = np.array([t["points"] * t.get("mult", 1) for t in trades])
    gross_profit = float(pts[pts > 0].sum())
    gross_loss = float(-pts[pts < 0].sum())
    equity = np.cumsum(pts)
    out = {
        "trades": len(trades),
        "wins": int((pts > 0).sum()),
        "win_rate": round(float((pts > 0).mean()), 4),
        "net_points": round(float(pts.sum()), 1),
        "avg_points": round(float(pts.mean()), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "max_dd_points": round(float((np.maximum.accumulate(equity) - equity).max()), 1),
    }
    # AI 成绩单补充(记录不评判): MAE/MFE 分位(调 SL/TP 依据) + 分年净点(识别时间集中)
    maes = [t["mae"] for t in trades if t.get("mae") is not None]
    if maes:
        out["mae_p90"] = round(float(np.percentile(maes, 90)), 1)
        out["mfe_p90"] = round(float(np.percentile(
            [t["mfe"] for t in trades if t.get("mfe") is not None], 90)), 1)
    by_year: dict = {}
    for t in trades:
        y = str(datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).year)
        by_year[y] = round(by_year.get(y, 0.0) + t["points"] * t.get("mult", 1), 1)
    out["by_year"] = by_year
    return out
