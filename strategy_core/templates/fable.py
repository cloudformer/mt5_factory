"""FABLE(寓言): Filtered Asymmetric Breakout, Loss-bounded Exposure
(2026-08-15 原创设计, Claude Fable 5 × Frank; 首个为本系统 regime 门定制的原生模板)

设计公理(为什么是这四件事, 缺一不可):
  1. 鞅性: 外汇短期方向不可预测 → 不赚预测的钱, 只赚【分布形状】的钱;
  2. 凸性: 截断左尾(k_sl×ATR 止损) + 放开右尾(rr=3~4 大目标) = 用止损复制期权凸性,
     成本是震荡期一串小额 -1R(期权费), 收益是趋势右尾(11745 已实证: 利润全在右尾);
  3. 趋势持续性(TSMOM)是外汇/贵金属里被两百年数据反复确认的少数真异象 —
     入场不预测只确认: Donchian n_break 根新极值 + buf×ATR 缓冲, 长均线+通道中线
     双结构过滤保证只顺大势; 一次性语义只做首根突破(追高不做, runner 无状态安全);
  4. regime 门 = 砍期权费: 亏损设计上集中在震荡格, 利润集中在趋势格 —
     全样本只求"亏不死+微正", 用 v0.3 门锁进趋势格上线, 门外空仓 theta 归零。

规则(全部收盘 bar 决策, 纯函数无状态):
  过滤  多: 收盘 > SMA(n_filter) 且 > n_filter 通道中线; 空镜像
  波动窗 ATR(atr_n) / 中位TR(n_filter) ∈ [vol_lo, vol_hi]: 死市没油不做, 危机点差不做
  入场  收盘首次突破前 n_break 根极值 ± buf_atr×ATR(前一根未突破才算"首次")
  出场  SL = k_sl×ATR(损失有界), TP = rr×k_sl×ATR(正偏引擎)

判读预登记(2026-08-15, 签字画押):
  · rr=3 → 平衡胜率 25%, rr=4 → 20%; 实测胜率超平衡线 >2 SE 才算有东西;
  · 象限画像验收: 趋势格 PF>1.3 且 ≥100 笔; 震荡格允许亏(亏了说明画像对);
  · 胜率天然 25~35%, 最大连亏 15~20 次在数学期望内 — 毙它用平衡线, 不用连亏;
  · 不保证盈利。保证的只有: 单笔损失有界(亏不死) + 假设可证伪(尺子说了算)。
建议周期 H1/H4(趋势尺度), 品种 XAUUSD/EURUSD。
"""
from typing import Optional

import numpy as np

from ..base import Signal, Strategy


class Fable(Strategy):
    PARAM_GRID = {
        "n_break": [20, 55],      # Donchian 突破回看(Turtle 经典对)
        "n_filter": [200],        # 长趋势结构过滤(SMA + 通道中线)
        "atr_n": [14],
        "k_sl": [2.0, 3.0],       # SL = k_sl×ATR(Turtle 2N 血统)
        "rr": [3.0, 4.0],         # TP = rr×风险距离(正偏引擎)
        "buf_atr": [0.25],        # 突破确认缓冲(ATR 倍数, 滤 tick 噪声假突破)
        "vol_lo": [0.5],          # ATR/中位TR 下限: 死市没油不做
        "vol_hi": [3.0],          # 上限: 危机乱纪元不做
    }
    RANDOM_SPACE = {
        "n_break": (10, 100, 5),
        "n_filter": (50, 400, 25),
        "atr_n": (7, 28, 1),
        "k_sl": (1.0, 4.0, 0.25),
        "rr": (1.5, 6.0, 0.25),
        "buf_atr": (0.0, 1.0, 0.05),
        "vol_lo": (0.0, 1.0, 0.05),
        "vol_hi": (1.5, 5.0, 0.25),
    }

    @classmethod
    def valid_params(cls, params):
        return (params["n_break"] >= 5 and params["n_filter"] >= params["n_break"]
                and params["atr_n"] >= 2 and params["k_sl"] > 0 and params["rr"] > 0
                and params["buf_atr"] >= 0
                and 0 <= params["vol_lo"] < params["vol_hi"])

    @property
    def warmup(self) -> int:
        p = self.params
        # TR 要前收错位一根; 中位TR/通道/SMA 都要满 n_filter; 一次性判定多看 2 根
        return max(p["n_filter"] + 1, p["n_break"] + 2) + p["atr_n"] + 5

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        p = self.params
        n_b, n_f, n_a = int(p["n_break"]), int(p["n_filter"]), int(p["atr_n"])
        cc = float(c[-1])

        # ATR = TR 简单均值(MT5 iATR 同口径, 与 weekly_day 一致)
        tr = np.maximum(h[1:] - l[1:],
                        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        atr = float(tr[-n_a:].mean())
        if atr <= 0:
            return None
        med = float(np.median(tr[-n_f:]))        # 长窗中位 TR = 本品种自己的常态刻度
        if med <= 0:
            return None
        ratio = atr / med
        if not (p["vol_lo"] <= ratio <= p["vol_hi"]):
            return None                           # 死市没油 / 危机乱纪元, 都不做

        # 长趋势结构过滤: 均线 + 通道中线双确认(同侧才有方向资格)
        sma = float(c[-n_f:].mean())
        mid = (float(h[-n_f:].max()) + float(l[-n_f:].min())) / 2.0
        long_ok = cc > sma and cc > mid
        short_ok = cc < sma and cc < mid
        if not (long_ok or short_ok):
            return None

        # Donchian 突破 + 一次性: 当前收盘破前 n_break 根极值 ± buf, 且前一根【未】破
        # 它自己的通道(错位一根) — 只做首根, 趋势中段不追(与 session_orb 同语义)
        buf = p["buf_atr"] * atr
        hh1 = float(h[-(n_b + 1):-1].max())
        ll1 = float(l[-(n_b + 1):-1].min())
        hh0 = float(h[-(n_b + 2):-2].max())
        ll0 = float(l[-(n_b + 2):-2].min())
        pc = float(c[-2])
        risk = p["k_sl"] * atr

        if long_ok and cc > hh1 + buf and not pc > hh0 + buf:
            return Signal("BUY", cc - risk, cc + p["rr"] * risk)
        if short_ok and cc < ll1 - buf and not pc < ll0 - buf:
            return Signal("SELL", cc + risk, cc - p["rr"] * risk)
        return None
