"""移动止损(v0.9): 持仓管理层的独立纯函数 — 回测引擎与实盘 runner 共用同一份。

设计定版(v0.9_移动止损.md):
- 作用对象=持仓(方向/入场价/当前SL), 与策略无关(策略类不知道它存在), BUY/SELL 镜像;
- 三类同一骨架: fixed(固定距离) / breakeven(先保本再追) / atr(距离=ATR×k 自适应);
- 零状态棘轮: new_sl = max(当前SL, 参考价−gap)(BUY; SELL 镜像 min/+gap) —
  SL 只向有利方向移, 当前 SL 即全部历史, 不记高水位、不回看;
- M1 粒度: 每根收盘 M1 评估一次(回测在 _walk_exit 循环里, 实盘 runner 每拍看最新收盘 M1);
- 参考价口径与撮合一致: BUY 用 bid(收盘价), SELL 用 ask(收盘价+当根点差)。

配置结构(策略 params["trail"], 全局默认 config.trail_default):
  {"active": "fixed"|"breakeven"|"atr"|null,   # null/缺失 = 不启用
   "fixed":     {"gap": 50},                    # gap: SL 距参考价多少点
   "breakeven": {"gap": 50, "start": 100},      # start: 盈利达到多少点才启动(必填>0)
   "atr":       {"k": 2.0, "period": 14},       # 距离 = M1 ATR(period) × k
   "keep_tp":   true}                           # false = 去掉TP让利润跑(默认true, TP不动)
"""
from typing import Optional

import numpy as np


def atr_m1(m1: dict, j: int, period: int) -> Optional[float]:
    """截至 M1 下标 j(含)的简单 ATR(价格单位); 历史不足 period+1 根返回 None"""
    if j + 1 < period + 1:
        return None
    h = m1["high"][j - period + 1: j + 1]
    l = m1["low"][j - period + 1: j + 1]
    cp = m1["close"][j - period: j]
    tr = np.maximum(h - l, np.maximum(np.abs(h - cp), np.abs(l - cp)))
    return float(tr.mean())


def trail_new_sl(direction: str, entry: float, cur_sl: Optional[float], ref: float,
                 cfg: dict, point: float, atr: Optional[float] = None) -> Optional[float]:
    """算"新 SL 应在哪"; 只在比当前更有利时返回新值, 否则 None(棘轮, 幂等, 零状态)。

    direction: "BUY"|"SELL"   entry: 入场价   cur_sl: 当前SL(铁律必有, 容 None)
    ref: 参考价(BUY=收盘bid, SELL=收盘ask)   cfg: trail 配置(见模块头)
    point: 品种最小报价单位   atr: 价格单位的 ATR(仅 active=atr 需要, 引擎/runner 现算传入)
    """
    t = (cfg or {}).get("active")
    if not t:
        return None
    p = cfg.get(t) or {}
    if t == "atr":
        k = p.get("k")
        if not k or atr is None or atr <= 0:
            return None
        gap = atr * float(k)
    else:
        gap_pts = p.get("gap")
        if not gap_pts or gap_pts <= 0:
            return None
        gap = float(gap_pts) * point
    start = float(p.get("start") or 0) * point   # 启动阈值(点→价格); 0=立即
    if t == "breakeven" and start <= 0:
        return None   # 保本类必须有启动阈值, 否则开仓瞬间SL=入场价=秒被点差扫掉
    if direction == "BUY":
        if ref - entry < start:
            return None                          # 未达启动盈利
        cand = ref - gap
        if t == "breakeven":
            cand = max(cand, entry)              # 启动即至少保本
        return cand if (cur_sl is None or cand > cur_sl) else None
    else:  # SELL 镜像: SL 只往下走
        if entry - ref < start:
            return None
        cand = ref + gap
        if t == "breakeven":
            cand = min(cand, entry)
        return cand if (cur_sl is None or cand < cur_sl) else None
