"""回测引擎: M1 回放 + 悲观撮合

撮合规则(CLAUDE.md 准入漏斗):
- 信号在 TF bar 收盘产生, 下一根 M1 开盘价成交
- BUY 以 ask 成交(bid + 当根点差 + 滑点), 以 bid 离场; SELL 反之
- SL/TP 用 M1 逐根检查; 同一根 M1 同时碰到 → 按止损算(悲观)
- 跳空穿价按实际开盘价成交, 不按挂单价
- 佣金按点数从每笔盈亏中扣除
"""
import logging
from datetime import datetime

import numpy as np

from strategy_core import TF_SECONDS, make_strategy

logger = logging.getLogger("backtest")

# 成本模型默认值 (可被回测请求参数覆盖)
DEFAULT_SLIPPAGE_POINTS = 3.0     # 单边滑点
DEFAULT_COMMISSION_POINTS = 7.0   # 往返佣金(点数等值)


def _spread_at(m1, j, point, spread_points):
    """当根点差(价格单位): 指定 spread_points 则固定点差, 否则用 bar 记录的真实点差"""
    return (spread_points if spread_points is not None else m1["spread"][j]) * point


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


def _walk_exit(pos, j_from, j_to, m1, point, spread_points):
    """M1 逐根检查 SL/TP。悲观: 先查SL后查TP; 跳空按开盘价。返回 (exit_price, j, reason) 或 None"""
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
    return None


def run_backtest(m1: dict, template: str, params: dict, point: float, timeframe: str,
                 slippage_points: float = DEFAULT_SLIPPAGE_POINTS,
                 commission_points: float = DEFAULT_COMMISSION_POINTS,
                 spread_points: float | None = None) -> dict:
    """单个策略实例回测, 返回 {metrics, trades}

    成本模型参数:
    - slippage_points:   单边滑点(点), 进场时向不利方向偏移
    - commission_points: 往返佣金(点数等值), 每笔盈亏中扣除
    - spread_points:     固定点差(点); None=用每根bar记录的真实点差(默认, 推荐)
    """
    strat = make_strategy(template, params, point)
    tf = aggregate(m1, TF_SECONDS[timeframe])
    w = strat.warmup
    n = len(tf["time"])
    slip = slippage_points * point
    commission = commission_points

    pos = None
    trades = []
    for i in range(w, n - 1):
        j_from, j_to = int(tf["m1_start"][i + 1]), int(tf["m1_end"][i + 1])

        if pos is None:
            sig = strat.on_bar(
                tf["open"][i - w + 1:i + 1], tf["high"][i - w + 1:i + 1],
                tf["low"][i - w + 1:i + 1], tf["close"][i - w + 1:i + 1],
            )
            if sig is None:
                continue
            j = j_from
            if sig.direction == "BUY":  # 买在 ask + 滑点
                entry = float(m1["open"][j] + _spread_at(m1, j, point, spread_points) + slip)
            else:  # 卖在 bid - 滑点
                entry = float(m1["open"][j] - slip)
            pos = {"dir": sig.direction, "entry": entry, "sl": sig.sl, "tp": sig.tp,
                   "entry_time": int(m1["time"][j])}

        hit = _walk_exit(pos, j_from, j_to, m1, point, spread_points)
        if hit:
            exit_price, j, reason = hit
            sign = 1 if pos["dir"] == "BUY" else -1
            points = sign * (exit_price - pos["entry"]) / point - commission
            trades.append({
                "dir": pos["dir"], "entry_time": pos["entry_time"],
                "exit_time": int(m1["time"][j]), "entry": round(pos["entry"], 6),
                "exit": round(exit_price, 6), "points": round(points, 1), "reason": reason,
            })
            pos = None

    metrics = _metrics(trades)
    metrics["settings"] = {  # 成本模型随结果存档, 不同设置的成绩不混淆
        "slippage_points": slippage_points,
        "commission_points": commission_points,
        "spread_points": spread_points if spread_points is not None else "recorded",
    }
    return {"metrics": metrics, "trades": trades}


def _metrics(trades: list) -> dict:
    if not trades:
        return {"trades": 0, "net_points": 0.0}
    pts = np.array([t["points"] for t in trades])
    gross_profit = float(pts[pts > 0].sum())
    gross_loss = float(-pts[pts < 0].sum())
    equity = np.cumsum(pts)
    return {
        "trades": len(trades),
        "wins": int((pts > 0).sum()),
        "win_rate": round(float((pts > 0).mean()), 4),
        "net_points": round(float(pts.sum()), 1),
        "avg_points": round(float(pts.mean()), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "max_dd_points": round(float((np.maximum.accumulate(equity) - equity).max()), 1),
    }
