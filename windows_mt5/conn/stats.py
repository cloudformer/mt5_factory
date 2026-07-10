"""交易统计采集 — runner 回传"账户 + 每策略战绩"用, bridge/runner 共享的只读模块。

采集两块:
  account_snapshot(): 账户余额/净值/浮动盈亏/保证金 (爆仓风险看 margin_level)
  per_strategy(instances): 每策略(按 magic) 当前持仓 + 已平仓成交统计

closed 统计基于 MT5 成交历史(最近 90 天), 较重 → 内部 60s 缓存;
positions/account 便宜, 每次实时取。runner 单线程调用, 无并发问题。
mt5 未连接时各函数返回 None/{}, 不抛异常 — 回传缺失显示为"—", 不影响交易主循环。
"""
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

HISTORY_DAYS = 90          # 已平仓统计的回看窗口
_HISTORY_TTL = 60          # 历史查询缓存秒数

_history_cache: dict = {"ts": 0.0, "by_magic": {}}


def account_snapshot():
    """账户快照; 未连接返回 None"""
    info = mt5.account_info()
    if info is None:
        return None
    return {
        "balance": info.balance,          # 余额(已实现)
        "equity": info.equity,            # 净值(含浮动)
        "profit": info.profit,            # 浮动盈亏
        "margin": info.margin,            # 已用保证金
        "margin_free": info.margin_free,  # 可用保证金
        "margin_level": info.margin_level,  # 保证金水平% (爆仓风险指标, 0=无持仓)
        "margin_so_call": info.margin_so_call,  # 追保线%
        "margin_so_so": info.margin_so_so,      # 强平线%
        "currency": info.currency,
    }


def _closed_by_magic() -> dict:
    """按 magic 汇总最近 HISTORY_DAYS 天的已平仓成交 (60s 缓存)"""
    now = time.time()
    if now - _history_cache["ts"] < _HISTORY_TTL:
        return _history_cache["by_magic"]
    since = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    deals = mt5.history_deals_get(since, datetime.now(timezone.utc)) or []
    by_magic: dict = {}
    for d in deals:
        if d.entry != mt5.DEAL_ENTRY_OUT:   # 只统计平仓腿(盈亏落在这条上)
            continue
        s = by_magic.setdefault(d.magic, {"trades": 0, "wins": 0, "profit": 0.0})
        s["trades"] += 1
        pnl = d.profit + d.commission + d.swap
        if pnl > 0:
            s["wins"] += 1
        s["profit"] += pnl
    for s in by_magic.values():
        s["profit"] = round(s["profit"], 2)
    _history_cache.update(ts=now, by_magic=by_magic)
    return by_magic


def per_strategy(instances: list, last_bar: dict | None = None) -> list:
    """每策略战绩: [{id, name, magic, last_bar, position:{...}, closed:{...}}]
    instances: runner 已加载的策略实例列表 (含 id/name/symbol/magic)
    last_bar: {策略id: 最后处理的收盘bar epoch} — bar 在走 = 正常盯盘, 停住 = 卡了"""
    closed = _closed_by_magic()
    last_bar = last_bar or {}
    out = []
    for inst in instances:
        magic = inst["magic"]
        si = mt5.symbol_info(inst["symbol"])   # 最新tick时间: 停滞=休市或报价断流
        positions = [p for p in (mt5.positions_get(symbol=inst["symbol"]) or [])
                     if p.magic == magic]
        out.append({
            "id": inst["id"],
            "name": inst["name"],
            "symbol": inst["symbol"],
            "magic": magic,
            "last_bar": last_bar.get(inst["id"]),
            "quote_ts": si.time if si else None,
            "position": {
                "count": len(positions),
                "volume": round(sum(p.volume for p in positions), 2),
                "profit": round(sum(p.profit for p in positions), 2),
            },
            "closed": closed.get(magic, {"trades": 0, "wins": 0, "profit": 0.0}),
        })
    return out
