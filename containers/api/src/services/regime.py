"""市场状态 Regime v1(v2.5, 2026-07-27 与 Frank 定) — 三维八格时间线。

三维: 长趋势(D1收盘 vs 长均线) / 短趋势(vs 短均线) / 波动(ATR_n vs 过去 win 日 q 分位)。
口径唯一源 = regime_versions 表(schema/053 版本化, v0.2 设计): 一套参数 = 一个 version
(params UNIQUE 判重), 当前默认版本指针在 config `regime_version`(active_version 自愈)。
每版本一套独立时间线(主键 version_id+symbol+date, 并存互不覆盖);
POST /regime/rebuild 对当前默认版本全量重算(同主键 UPSERT + 修剪头部残留)。
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


def label(p: dict) -> str:
    """版本标签唯一来源(2026-08-10 Frank 定"改一个地方全出来"): 五参数全显 —
    只显示 长/短/ATR 曾让 v2 与 v4(仅分位不同)在页面上撞名"""
    p = p or {}
    return (f"{p.get('long_ma', '?')}/{p.get('short_ma', '?')}/ATR{p.get('atr_n', '?')}"
            f"/{p.get('vol_win', '?')}日/q{p.get('vol_q', '?'):g}"
            if isinstance(p.get('vol_q'), (int, float)) else
            f"{p.get('long_ma', '?')}/{p.get('short_ma', '?')}/ATR{p.get('atr_n', '?')}")
_MA_RE = re.compile(r"^(sma|ema)(\d{1,3})$")


async def active_version(pool: asyncpg.Pool) -> tuple[int, dict]:
    """当前默认版本 (config regime_version → regime_versions.params), 口径唯一源(v0.2)。
    自愈: 配置指的版本被手工删库(页面无删除口, 删除=Frank 直接 DELETE) → 回落最小 id
    并写回 config; 表被清空则种回默认参数 — 任何状态下都能给出可用版本, 不脆弱。"""
    vid = await pool.fetchval(
        "SELECT (value #>> '{}')::int FROM config WHERE key='regime_version'")
    p = None
    if vid:
        p = await pool.fetchval("SELECT params FROM regime_versions WHERE id=$1", vid)
    if p is None:
        row = await pool.fetchrow(
            "SELECT id, params FROM regime_versions ORDER BY id LIMIT 1")
        if row is None:
            row = await pool.fetchrow(
                "INSERT INTO regime_versions (params) VALUES ($1) RETURNING id, params",
                DEFAULT_PARAMS)
        vid, p = row["id"], row["params"]
        await pool.execute(
            "INSERT INTO config (key, value) VALUES ('regime_version', $1)"
            " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", vid)
        logger.warning("regime_version 自愈回落 → v%d", vid)
    return vid, {**DEFAULT_PARAMS, **p}


async def tl_map(pool: asyncpg.Pool, symbol: str, version_id: int | None = None) -> dict:
    """{入场日: 格子} 映射 — 读时贴格唯一入口(分析/对账/矩阵/tsl 共用)。
    version_id 不传=当前默认版本; 指定=看该版本的天气(矩阵页版本下拉);
    指定版本无时间线 → 空映射(全部无标签, 如实报, 不脆弱)"""
    if version_id is None:
        version_id, _ = await active_version(pool)
    return {r["date"]: r["regime"] for r in await pool.fetch(
        "SELECT date, regime FROM regime_timeline WHERE version_id=$1 AND symbol=$2",
        version_id, symbol)}


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


async def rebuild_symbol(pool: asyncpg.Pool, symbol: str, params: dict,
                         version_id: int) -> str | None:
    """按指定版本口径全量重算一个品种: 覆盖更新(同主键 UPSERT) + 修剪头部残留
    (换更长暖机的口径后, 新起点之前的旧行没人覆盖会留旧口径值 → 修剪, 保持数据干净)。
    只动本版本的行, 其他版本时间线不受影响。返回 None=成功 / 原因字符串(数据不足等)。"""
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
                "INSERT INTO regime_timeline"
                " (version_id, symbol, date, long_trend, short_trend, vol)"
                " VALUES ($1, $2, $3, $4, $5, $6)"
                " ON CONFLICT (version_id, symbol, date) DO UPDATE SET"
                "   long_trend = EXCLUDED.long_trend, short_trend = EXCLUDED.short_trend,"
                "   vol = EXCLUDED.vol",
                [(version_id, symbol, dates[i], dims[0][i], dims[1][i], dims[2][i])
                 for i in range(start, len(dates))])
            await conn.execute(   # 头部修剪: 新暖机起点之前的行是旧口径残留(只剪本版本)
                "DELETE FROM regime_timeline"
                " WHERE version_id=$1 AND symbol=$2 AND date < $3",
                version_id, symbol, dates[start])
    logger.info("regime timeline rebuilt: v%d %s %d days (%s → %s) params=%s",
                version_id, symbol, len(dates) - start, dates[start], dates[-1], params)
    return None


async def ensure_timeline(pool: asyncpg.Pool, symbol: str,
                          version_id: int | None = None) -> str | None:
    """读时自愈(无定时任务): timeline 落后于库内 M1 最新交易日才重算 —
    每天最多一次, 新鲜时零开销。换口径的即时重算走 POST /regime/rebuild(显式动作)。
    version_id 不传=治当前默认版本; 指定=治该版本(矩阵页切版本 → 切谁治谁,
    第一次切新版本自动建齐, 慢一次以后秒开)。"""
    last_bar = await pool.fetchval(
        "SELECT max(time)::date FROM historical_bars WHERE symbol=$1 AND timeframe='M1'",
        symbol)
    if last_bar is None:
        return f"{symbol} 库内无 M1 数据 — 先去「数据」页下载"
    if version_id is None:
        vid, params = await active_version(pool)
    else:
        p = await pool.fetchval(
            "SELECT params FROM regime_versions WHERE id=$1", version_id)
        if p is None:
            return f"版本 v{version_id} 不存在"
        vid, params = version_id, {**DEFAULT_PARAMS, **p}
    last_tl = await pool.fetchval(
        "SELECT max(date) FROM regime_timeline WHERE version_id=$1 AND symbol=$2",
        vid, symbol)
    if last_tl is not None and last_tl >= last_bar:
        return None
    return await rebuild_symbol(pool, symbol, params, vid)


async def gate_for(pool: asyncpg.Pool, metadata, symbol: str) -> dict | None:
    """策略 metadata → 回测引擎的 regime 门(v0.3): {"cells": {格:倍率}, "tl": {日期:格}}。
    无门(空 metadata) → None(引擎走原路径)。版本钉死在 metadata 里(不跟全局默认);
    顺手自愈该版本该品种的时间线(job 在后台跑, 正是建时间线的好时机)。"""
    g = metadata.get("regime") if isinstance(metadata, dict) else None
    if not (isinstance(g, dict) and isinstance(g.get("cells"), dict) and g["cells"]):
        return None
    vid = int(g["version"])
    try:
        await ensure_timeline(pool, symbol, vid)
    except Exception as e:   # 时间线建不出(历史不足等): 门照常生效, 无格日=不开仓, 如实
        logger.warning("gate ensure v%d %s failed: %s", vid, symbol, e)
    return {"cells": g["cells"], "tl": await tl_map(pool, symbol, vid)}


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
    """标准③ 描述性区分度(同期性格, 非预测 — 2026-07-29 与 Frank 定的收口):
    regime 只描述"今天什么天气", 不预测明天赚不赚(那是策略的事, 且有效市场测不出)。
    区分度 = 标签是否对应**真实的同期市场性格差异**, 不是能否预测未来收益。
    - 波动: 高/低波格 同期日均真实波幅比(高波格当天确实更颠 → 波动标签名副其实);
    - 趋势: 牛/熊态 同期日收益均值差 t 值(牛市那些天市场当天确实在往上走 → 趋势标签属实)。
    两者都刻画"当天性格", 零前瞻。此前用"前瞻/次日收益 t"是错尺子(逼 regime 做预测),
    全线测不出后又被迫换维度=不知目的地的乱导航 — 已废弃, 见 v2.5 文档收口记录。
    单品种记分卡与候选族对比(evaluate)共用同一算法。"""
    if dims is None or len(c) - start <= 300:
        return None
    tr = (h[start:] - l[start:]) / c[start:] * 100
    va = dims[2][start:] == "A"
    la = dims[0][start:] == "A"
    ret = np.diff(np.log(c[start:]))   # 相邻收盘的日收益; ret[i] = 第 i→i+1 天
    lab = la[1:]                       # 与 ret 对齐: 用当日(i+1)的趋势态标注当日的收益
    ra, rb = ret[lab], ret[~lab]       # 牛态 / 熊态 各自的同期日收益
    se = (np.sqrt(ra.var(ddof=1) / len(ra) + rb.var(ddof=1) / len(rb))
          if len(ra) > 30 and len(rb) > 30 else 0)
    return {"vol_ratio": round(float(tr[va].mean() / tr[~va].mean()), 2)
                         if va.any() and (~va).any() else None,
            "trend_t": round(float((ra.mean() - rb.mean()) / se), 1) if se else None}
