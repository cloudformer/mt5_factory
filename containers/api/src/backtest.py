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

COSTS = {"slippage_points": 3, "commission_points": 7}  # 单边滑点 / 往返佣金


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


def _walk_exit(pos, j_from, j_to, m1, point):
    """M1 逐根检查 SL/TP。悲观: 先查SL后查TP; 跳空按开盘价。返回 (exit_price, j, reason) 或 None"""
    o, h, l = m1["open"], m1["high"], m1["low"]
    sp = m1["spread"]
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
            ask_o = o[j] + sp[j] * point
            if ask_o >= pos["sl"]:
                return float(ask_o), j, "sl_gap"
            if h[j] + sp[j] * point >= pos["sl"]:
                return pos["sl"], j, "sl"
            if ask_o <= pos["tp"]:
                return float(ask_o), j, "tp_gap"
            if l[j] + sp[j] * point <= pos["tp"]:
                return pos["tp"], j, "tp"
    return None


def run_backtest(m1: dict, template: str, params: dict, point: float, timeframe: str) -> dict:
    """单个策略实例回测, 返回 {metrics, trades}"""
    strat = make_strategy(template, params, point)
    tf = aggregate(m1, TF_SECONDS[timeframe])
    w = strat.warmup
    n = len(tf["time"])
    slip = COSTS["slippage_points"] * point
    commission = COSTS["commission_points"]

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
                entry = float(m1["open"][j] + m1["spread"][j] * point + slip)
            else:  # 卖在 bid - 滑点
                entry = float(m1["open"][j] - slip)
            pos = {"dir": sig.direction, "entry": entry, "sl": sig.sl, "tp": sig.tp,
                   "entry_time": int(m1["time"][j])}

        hit = _walk_exit(pos, j_from, j_to, m1, point)
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

    return {"metrics": _metrics(trades), "trades": trades}


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
