"""SQUEEZE(压缩突破): 第一个入场时机本身就是 regime 波动维的模板
(2026-08-16 原创设计, Claude Fable 5 × Frank)

与 regime 的同构关系: 波动是均值回归的(低波之后必有高波 — 比方向更硬的统计事实)。
压缩判定 ATR/中位TR ≤ sq_ratio ≅ regime 波动维的 B 格(低波) — 入场日应天然集中在
xxB 格, 吃 B→A 的波动扩张。注意与 FABLE 的互补: FABLE 的 vol_lo 把死市【跳过】,
本模板反过来在死市【蹲守】突破 — 同一个维度, 两种收割方式。

规则(全部收盘 bar 决策, 纯函数无状态):
  压缩  前一根收盘时 ATR(atr_n) / 中位TR(n_ref) ≤ sq_ratio(不含当前bar —
        突破 bar 自己的大波幅不许污染压缩判定, 否则永远等不到入场)
  突破  收盘破前 n_channel 根极值 ± buf_atr×ATR, 方向 = 突破方向;
        trend_filter=1 时还须与长线(SMA n_ref)同向
  一次性 前一根未破它自己的通道(只做首根, 与 fable 同语义)
  出场  SL = k_sl×ATR(含突破bar的新鲜波幅, 风险如实放大), TP = rr×风险

判读预登记(2026-08-16, 签字画押):
  · 死刑条款: 入场日必须集中在低波格(xxB); metadata 显示入场散在高波格 = 实现错, 直接删;
  · rr=2 → 平衡胜率 33.3%, rr=3 → 25%; 超平衡线 >2 SE 才算有东西;
  · 先验中性: squeeze 在外汇的公开证据一般, 黄金稍好 — 两个品种都可能死, 死了就删;
  · 不保证盈利, 保证单笔损失有界 + 可证伪。
建议周期 H1/H4, 品种 XAUUSD, EURUSD(波动回归是跨品种性质, 欧元这次不是陪跑)。
"""
from typing import Optional

import numpy as np

from ..base import Signal, Strategy


class Squeeze(Strategy):
    PARAM_GRID = {
        "n_ref": [200],           # 常态刻度窗(中位TR + 可选长线过滤)
        "n_channel": [20],        # 压缩区间 = Donchian 通道
        "atr_n": [14],
        "sq_ratio": [0.5, 0.7],   # 压缩门槛: ATR/中位TR ≤ 此值才算蹲守成立
        "buf_atr": [0.25],        # 突破确认缓冲
        "k_sl": [2.0],
        "rr": [2.0, 3.0],
        "trend_filter": [0, 1],   # 1=只顺长线方向突破
    }
    RANDOM_SPACE = {
        "n_ref": (100, 400, 25),
        "n_channel": (10, 60, 5),
        "atr_n": (7, 28, 1),
        "sq_ratio": (0.3, 0.9, 0.05),
        "buf_atr": (0.0, 1.0, 0.05),
        "k_sl": (1.0, 4.0, 0.25),
        "rr": (1.0, 5.0, 0.25),
        "trend_filter": (0, 1, 1),
    }

    @classmethod
    def valid_params(cls, params):
        return (params["n_ref"] >= params["n_channel"] >= 5
                and 0 < params["sq_ratio"] < 1 and params["atr_n"] >= 2
                and params["buf_atr"] >= 0 and params["k_sl"] > 0
                and params["rr"] > 0 and params["trend_filter"] in (0, 1))

    @property
    def warmup(self) -> int:
        p = self.params
        return p["n_ref"] + p["atr_n"] + 5

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        p = self.params
        n_ch, n_a = int(p["n_channel"]), int(p["atr_n"])
        cc, pc = float(c[-1]), float(c[-2])

        tr = np.maximum(h[1:] - l[1:],
                        np.abs(np.stack([h[1:] - c[:-1], l[1:] - c[:-1]])).max(axis=0))
        # 压缩判定不含当前 bar: 突破 bar 自己的大波幅不许污染"此前在蹲守"这个事实
        atr_prior = float(tr[:-1][-n_a:].mean())
        med = float(np.median(tr[:-1][-int(p["n_ref"]):]))
        if atr_prior <= 0 or med <= 0:
            return None
        if atr_prior / med > p["sq_ratio"]:
            return None                                 # 没在压缩, 无能可蓄

        atr_now = float(tr[-n_a:].mean())               # SL 用含突破 bar 的新鲜风险
        buf = p["buf_atr"] * atr_prior
        hh1 = float(h[-(n_ch + 1):-1].max())            # 当前 bar 的通道(不含自己)
        ll1 = float(l[-(n_ch + 1):-1].min())
        hh0 = float(h[-(n_ch + 2):-2].max())            # 前一 bar 的通道(一次性判定)
        ll0 = float(l[-(n_ch + 2):-2].min())

        sma = float(c[-int(p["n_ref"]):].mean())
        allow_up = (not p["trend_filter"]) or cc > sma
        allow_dn = (not p["trend_filter"]) or cc < sma
        risk = p["k_sl"] * atr_now

        if allow_up and cc > hh1 + buf and not pc > hh0 + buf:
            return Signal("BUY", cc - risk, cc + p["rr"] * risk)
        if allow_dn and cc < ll1 - buf and not pc < ll0 - buf:
            return Signal("SELL", cc + risk, cc - p["rr"] * risk)
        return None
