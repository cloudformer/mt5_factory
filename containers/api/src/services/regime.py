"""市场状态 Regime v1(v2.5, 2026-07-27 与 Frank 定) — 三维八格时间线。

三维: 长趋势(D1收盘 vs 长均线) / 短趋势(vs 短均线) / 波动(ATR_n vs 过去 win 日 q 分位)。
口径在 config 表 `regime_params`(schema/048, 配置页可改 — **评定期专用**):
    {"long_ma": "sma200", "short_ma": "sma20", "atr_n": 14, "vol_win": 252, "vol_q": 0.5}
改口径 → POST /regime/rebuild 全量重算**覆盖更新**(同主键 UPSERT + 修剪新暖机起点前的
头部残留行) — 不删数据表, 永远只有当前口径的一份干净数据。
铁纪律: 第 t 日的格子只用**截至 t-1 收盘**的数据(右移一位, 无未来函数);
       D1 从 M1 按券商服务器时间日界聚合; 禁止用策略盈利调口径(循环论证)。
存储: regime_timeline 三维各一列 + regime 生成列(库执法拼接, 见 schema/047);
     读时自愈(ensure_timeline), 无定时任务。
"""
import logging
import re

import asyncpg
import numpy as np

logger = logging.getLogger("regime")

DEFAULT_PARAMS = {"long_ma": "sma200", "short_ma": "sma20",
                  "atr_n": 14, "vol_win": 252, "vol_q": 0.5}
CELLS = ("AAA", "AAB", "ABA", "ABB", "BAA", "BAB", "BBA", "BBB")
_MA_RE = re.compile(r"^(sma|ema)(\d{1,3})$")


async def load_params(pool: asyncpg.Pool) -> dict:
    """口径唯一源 = config 表(schema/048 种子); 缺项用默认补齐(容错, 不静默换口径)"""
    v = await pool.fetchval("SELECT value FROM config WHERE key='regime_params'")
    return {**DEFAULT_PARAMS, **(v or {})}


def parse_ma(spec: str):
    """'sma200'/'ema50' → (kind, n); 非法给明确原因(配置校验也在 api 侧把关, 这里兜底)"""
    m = _MA_RE.match(str(spec).strip().lower())
    if not m:
        raise ValueError(f"均线口径非法: {spec!r} — 应为 sma/ema + 周期, 如 sma200 / ema50")
    return m.group(1), int(m.group(2))


def warmup_days(params: dict) -> int:
    """有效格子前的最少交易日: 最长均线 与 ATR+波动窗 取大, 加余量"""
    n_long = parse_ma(params["long_ma"])[1]
    n_short = parse_ma(params["short_ma"])[1]
    return max(n_long, n_short, params["atr_n"] + params["vol_win"]) + 10


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(x, dtype=float)
        out[n - 1:] = (c[n - 1:] - np.concatenate(([0.0], c[:-n]))) / n
    return out


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    k = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def _ma(x: np.ndarray, spec: str) -> np.ndarray:
    kind, n = parse_ma(spec)
    return _sma(x, n) if kind == "sma" else _ema(x, n)


def _atr(h: np.ndarray, low: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    tr = np.maximum(h[1:], c[:-1]) - np.minimum(low[1:], c[:-1])   # 真实波幅(含跳空)
    tr = np.concatenate(([h[0] - low[0]], tr))
    return _sma(tr, n)


def _rolling_q(x: np.ndarray, win: int, q: float) -> np.ndarray:
    """滚动分位(只看过去 win 根, 不含当日 — 无未来函数)"""
    out = np.full(len(x), np.nan)
    for i in range(win, len(x)):
        w = x[i - win:i]
        w = w[~np.isnan(w)]
        if len(w) >= win // 2:
            out[i] = np.quantile(w, q)
    return out


def compute_regimes(h: np.ndarray, low: np.ndarray, c: np.ndarray, params: dict):
    """(D1 序列, 口径) → (三维字符数组列表 [long, short, vol], 有效起始下标)。
    第 t 日用截至 t-1 收盘的数据(整体右移一位)。"""
    ma_l = _ma(c, params["long_ma"])
    ma_s = _ma(c, params["short_ma"])
    atr = _atr(h, low, c, params["atr_n"])
    vth = _rolling_q(atr, params["vol_win"], params["vol_q"])
    a_long = np.roll(c > ma_l, 1)
    a_short = np.roll(c > ma_s, 1)
    a_vol = np.roll(atr > vth, 1)
    valid = np.roll(~(np.isnan(ma_l) | np.isnan(ma_s) | np.isnan(vth)), 1)
    valid[0] = False
    if not valid.any():
        return None, len(c)
    start = int(np.argmax(valid))
    dims = [np.where(a, "A", "B") for a in (a_long, a_short, a_vol)]
    return dims, start


async def _d1(pool: asyncpg.Pool, symbol: str, min_days: int):
    """D1 双源合并(2026-07-29 与 Frank 定"M1+D1 两层"): 有 M1 的日子用 M1 现场聚合
    (与交易/回测同源, 券商时间日界); 更早的头部用原生 D1 行补
    (MetaQuotes M1 仅存~4个月而 D1 有16年+ — regime 长视野靠它)。
    重叠日以 M1 聚合优先; 不足 min_days 返回 None"""
    rows = await pool.fetch(
        "WITH m1 AS (SELECT time::date AS d, max(high) AS h, min(low) AS l,"
        "                   (array_agg(close ORDER BY time DESC))[1] AS c"
        "              FROM historical_bars WHERE symbol=$1 AND timeframe='M1'"
        "             GROUP BY 1)"
        " SELECT d, h, l, c FROM m1"
        " UNION ALL"
        " SELECT time::date, high, low, close FROM historical_bars"
        "  WHERE symbol=$1 AND timeframe='D1'"
        "    AND time::date < COALESCE((SELECT min(d) FROM m1), 'infinity'::date)"
        " ORDER BY d", symbol)
    if len(rows) < min_days:
        return None
    return ([r["d"] for r in rows],
            np.array([float(r["h"]) for r in rows]),
            np.array([float(r["l"]) for r in rows]),
            np.array([float(r["c"]) for r in rows]))


async def rebuild_symbol(pool: asyncpg.Pool, symbol: str, params: dict) -> str | None:
    """按当前口径全量重算一个品种: 覆盖更新(同主键 UPSERT) + 修剪头部残留
    (换更长暖机的口径后, 新起点之前的旧行没人覆盖会留旧口径值 → 修剪, 保持数据干净)。
    返回 None=成功 / 原因字符串(数据不足等)。"""
    need = warmup_days(params)
    d1 = await _d1(pool, symbol, need)
    if d1 is None:
        return (f"{symbol} 交易日不足(当前口径需 ≥{need} 天暖机:"
                f" {params['long_ma']}/{params['short_ma']}/ATR{params['atr_n']}"
                f"+{params['vol_win']}日窗) — 下载更长历史后自动可用")
    dates, h, low, c = d1
    dims, start = compute_regimes(h, low, c, params)
    if dims is None or start >= len(dates):
        return f"{symbol} 暖机后无有效交易日"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                "INSERT INTO regime_timeline (symbol, date, long_trend, short_trend, vol)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (symbol, date) DO UPDATE SET"
                "   long_trend = EXCLUDED.long_trend, short_trend = EXCLUDED.short_trend,"
                "   vol = EXCLUDED.vol",
                [(symbol, dates[i], dims[0][i], dims[1][i], dims[2][i])
                 for i in range(start, len(dates))])
            await conn.execute(   # 头部修剪: 新暖机起点之前的行是旧口径残留
                "DELETE FROM regime_timeline WHERE symbol=$1 AND date < $2",
                symbol, dates[start])
    logger.info("regime timeline rebuilt: %s %d days (%s → %s) params=%s",
                symbol, len(dates) - start, dates[start], dates[-1], params)
    return None


async def ensure_timeline(pool: asyncpg.Pool, symbol: str) -> str | None:
    """读时自愈(无定时任务): timeline 落后于库内 M1 最新交易日才重算 —
    每天最多一次, 新鲜时零开销。换口径的即时重算走 POST /regime/rebuild(显式动作)。"""
    last_bar = await pool.fetchval(
        "SELECT max(time)::date FROM historical_bars WHERE symbol=$1 AND timeframe='M1'",
        symbol)
    if last_bar is None:
        return f"{symbol} 库内无 M1 数据 — 先去「数据」页下载"
    last_tl = await pool.fetchval(
        "SELECT max(date) FROM regime_timeline WHERE symbol=$1", symbol)
    if last_tl is not None and last_tl >= last_bar:
        return None
    return await rebuild_symbol(pool, symbol, await load_params(pool))


def _runs(seq: list) -> list:
    runs, cur = [], 1
    for prev, now in zip(seq[:-1], seq[1:]):
        if now == prev:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    return runs


def _dwell_w(runs: list) -> float:
    """按天加权中位格龄(2026-07-29 与 Frank 定, 指标改良): "随便挑一天, 它所在段有多长"
    的中位数 — 按段数的朴素中位会被穿越期碎段淹没(XAUUSD 实证: SMA200 长牛大段动辄
    数百天, 但穿越抖动制造大量 2~3 天碎段, 按段中位只剩 5 天, 完全失真)。
    实现 = 段长的加权中位(权重=段长): 排序后取累计天数过半处的段长。"""
    lens = sorted(runs)
    half = sum(lens) / 2
    acc = 0
    for ln in lens:
        acc += ln
        if acc >= half:
            return float(ln)
    return float(lens[-1])


def stats(regimes: list[str]) -> dict:
    """四标准原始值(v2.5 口径评定用, 全中性指标) — 页面即记分卡。
    格龄以按天加权中位(dwell/combo_median)为判定值; 按段朴素中位(_seg 后缀)留作参考。"""
    n = len(regimes)
    if n < 30:
        return {"days": n}
    dims = [[r[i] for r in regimes] for i in range(3)]
    years = n / 252.0
    combo_runs = _runs(regimes)
    cells = {k: round(regimes.count(k) / n * 100, 1) for k in CELLS}
    agree = {"长vs短": round(sum(a == b for a, b in zip(dims[0], dims[1])) / n * 100),
             "长vs波": round(sum(a == b for a, b in zip(dims[0], dims[2])) / n * 100),
             "短vs波": round(sum(a == b for a, b in zip(dims[1], dims[2])) / n * 100)}
    dim_runs = {name: _runs(d) for name, d in zip(("long", "short", "vol"), dims)}
    return {
        "days": n, "years": round(years, 1),
        # 判定值 = 按天加权; _seg = 按段朴素中位(参考, 页面括注, 观察一段时间后退役)
        "dwell": {name: _dwell_w(r) for name, r in dim_runs.items()},
        "dwell_seg": {name: float(np.median(r)) for name, r in dim_runs.items()},
        "combo_median": _dwell_w(combo_runs),
        "combo_median_seg": float(np.median(combo_runs)),
        "flips_per_year": round((len(combo_runs) - 1) / years, 1),
        "cells": cells, "cov_min": min(cells.values()), "cov_max": max(cells.values()),
        "agree": agree, "agree_max": max(agree.values()),
        "current_run_days": combo_runs[-1],
    }


def distinct(h, l, c, dims, start) -> dict | None:
    """标准③区分度(中性指标): 高/低波格日均真实波幅比 + 长趋势A/B次日收益t值。
    从 routes 内联提出来(2026-07-29): 单品种记分卡与候选族对比(evaluate)共用同一算法。"""
    if dims is None or len(c) - start <= 300:
        return None
    tr = (h[start:] - l[start:]) / c[start:] * 100
    va = dims[2][start:] == "A"
    la = dims[0][start:] == "A"
    ret = np.concatenate((np.diff(np.log(c[start:])), [np.nan]))
    ra, rb = ret[la & ~np.isnan(ret)], ret[~la & ~np.isnan(ret)]
    se = (np.sqrt(ra.var(ddof=1) / len(ra) + rb.var(ddof=1) / len(rb))
          if len(ra) > 30 and len(rb) > 30 else 0)
    return {"vol_ratio": round(float(tr[va].mean() / tr[~va].mean()), 2)
                         if va.any() and (~va).any() else None,
            "trend_t": round(float((ra.mean() - rb.mean()) / se), 1) if se else None}
