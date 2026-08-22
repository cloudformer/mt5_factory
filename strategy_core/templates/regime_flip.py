"""REGIME_FLIP(格龄赌翻转): 检验"转移概率可交易"假设的处刑台
(2026-08-22 Frank 提出, Claude Fable 5 落码)

假设来源: 八格转移统计显示格子结束后 ~80% 走前两名去向, 其中"短趋势维翻转"主导
(全库 10 品种 × 各口径版本一致)。本模板把它做成可证伪的策略: 短趋势状态持续满
min_dwell 根后, 朝"短趋势翻转"的方向开一注, ATR 括号出场。

与 regime 的同构关系(模板只看 bar, 不读 timeline — 回测/实盘同一函数):
  长维 ≅ 收盘 vs SMA(n_long);  短维 ≅ 收盘 vs SMA(n_short);
  格龄 ≅ 短维状态的连续 bar 数(建议跑 D1 = 与 regime 日粒度同构, H4/H1 = 更细的同型)。

规则(全部收盘 bar 决策, 纯函数无状态, 每段状态只赌一注):
  状态  短维 = 收盘 > SMA(n_short) ? A : B, 数出当前状态连续根数 dwell
  入场  dwell == min_dwell 那一根收盘: 短维 A → SELL(赌翻空), B → BUY(赌翻多)
  过滤  align=1 只保留与长维同向的注(长↑只买短维回调 = 顺长逆短);
        align=0 全赌(含逆长势的注)
  出场  SL = k_sl×ATR(atr_n), TP = rr×SL(纯 ATR 括号 — 假设本身没有结构位)

判读预登记(2026-08-22, 签字画押 — 落码者押注: 这个策略会死):
  · 死因预测: 格子标签是价格的滞后函数(SMA 交叉), "标签将翻"的信息在标签翻的
    那天已经被价格走完 — 转移概率预测的是滞后指标自己, 不含未来价格;
  · 平衡线: rr=1.5 → 平衡胜率 40%, rr=3 → 25%; 超 2 SE 才算有东西;
  · 若 align=1 活了: 那是"顺势回调低吸"借尸还魂(PULLBACK 的粗糙表亲), 不算
    转移概率的胜利 — 对照组 align=0 必须同时活才能归因给转移结构;
  · 证伪即删不调参救; 不保证盈利, 保证单笔损失有界 + 可证伪。
建议周期 D1/H4, 品种 AUDUSD/XAUUSD(+EURUSD 对照)。
"""
from typing import Optional

import numpy as np

from ..base import Signal, Strategy


class RegimeFlip(Strategy):
    PARAM_GRID = {
        "n_long": [200],          # 长维(与 regime v1 口径同构)
        "n_short": [20],          # 短维
        "atr_n": [14],
        "min_dwell": [5, 10, 20],  # 格龄门槛: 状态持续满 N 根才赌翻转
        "k_sl": [2.0, 3.0],       # SL = k_sl×ATR
        "rr": [1.5, 3.0],         # TP = rr×SL
        "align": [0, 1],          # 1=只顺长维方向的注; 0=全赌
    }
    RANDOM_SPACE = {
        "n_long": (50, 400, 25),
        "n_short": (5, 60, 5),
        "atr_n": (7, 28, 1),
        "min_dwell": (2, 60, 1),
        "k_sl": (1.0, 5.0, 0.5),
        "rr": (1.0, 5.0, 0.25),
        "align": (0, 1, 1),
    }

    @classmethod
    def valid_params(cls, params):
        return (params["n_long"] >= params["n_short"] >= 2
                and params["min_dwell"] >= 2 and params["atr_n"] >= 2
                and params["k_sl"] > 0 and params["rr"] > 0
                and params["align"] in (0, 1))

    @property
    def warmup(self) -> int:
        p = self.params
        return int(p["n_long"]) + int(p["min_dwell"]) + int(p["atr_n"]) + 5

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        p = self.params
        n_s, dwell_need = int(p["n_short"]), int(p["min_dwell"])
        cc = float(c[-1])

        # 短维状态序列(最近 dwell_need+1 根): 每根用它自己截止的 SMA(n_short)
        # — 与"当天收盘判定"的 regime 口径同构, 无未来函数
        k = dwell_need + 1
        states = []
        for j in range(-k, 0):
            sma_j = float(c[j - n_s + 1: j + 1 if j != -1 else None].mean())
            states.append(float(c[j]) > sma_j)
        # 格龄恰好 == min_dwell 才开注(每段状态只赌一次, 不重复下注同一段)
        cur = states[-1]
        if any(s != cur for s in states[1:]) or states[0] == cur:
            return None

        tr = np.maximum(h[1:] - l[1:],
                        np.abs(np.stack([h[1:] - c[:-1], l[1:] - c[:-1]])).max(axis=0))
        atr = float(tr[-int(p["atr_n"]):].mean())
        if atr <= 0:
            return None

        long_up = cc > float(c[-int(p["n_long"]):].mean())
        risk = p["k_sl"] * atr
        if cur:          # 短维 A(在短均线上方持续满 N 根) → 赌翻空
            if p["align"] and long_up:   # 顺长过滤: 长↑不做空
                return None
            return Signal("SELL", cc + risk, cc - p["rr"] * risk)
        else:            # 短维 B → 赌翻多
            if p["align"] and not long_up:
                return None
            return Signal("BUY", cc - risk, cc + p["rr"] * risk)
