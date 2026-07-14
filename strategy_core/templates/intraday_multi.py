"""多因子门控模板: 顺势回调(trend-pullback), 日内多笔(M5/M15)

三族指标各取一, 互补不同族(方向/时机/环境), 避免同族叠加的假多样性:
- 趋势闸(MA, 可开关):   收盘在慢线上方才做多、下方才做空 — 定方向
- 时机触发(RSI 回穿):   上升趋势里 RSI 回落后重新上穿 rsi_buy → BUY;
                        下降趋势里 RSI 冲高后回落下穿 rsi_sell → SELL — 定进场点。
                        "穿越"是事件, 天然稀疏 → 不需要带状态的冷却器,
                        on_bar 保持纯函数(回测/实盘调用节奏不同, 有状态会两边漂移)
- 环境闸(ATR, 可开关):  当前波动 ≥ 常态波动×atr_gate 才做 — 行情死的时候不玩
- 出场:                 SL = ATR×sl_atr(随波动自适应, 跨品种天然通用), TP = SL×rr

每关一个闸 = 少一个指标的变体 → 一个模板覆盖整个家族, 组合空间交给随机生成 + OOS/跨品种双筛。
"""
from typing import Optional

import numpy as np

from ..base import Signal, Strategy


def _rsi(c, n: int) -> float:
    """简单均值 RSI(窗口内确定性计算, 无状态 — 回测/实盘同值)"""
    diff = np.diff(c[-(n + 1):])
    up = diff[diff > 0].sum() / n
    dn = -diff[diff < 0].sum() / n
    if dn == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + up / dn)


def _atr(h, l, c, n: int) -> float:
    """真实波幅均值(简单均值, 无状态)"""
    hh, ll, cp = h[-n:], l[-n:], c[-n - 1:-1]
    tr = np.maximum(hh - ll, np.maximum(np.abs(hh - cp), np.abs(ll - cp)))
    return float(tr.mean())


class IntradayMulti(Strategy):
    PARAM_GRID = {  # 小型健全网格(冒烟/网格模式用); 真正的空间在 RANDOM_SPACE
        "use_trend": [0, 1],
        "ma_slow": [50, 100],
        "rsi_period": [7, 14],
        "rsi_buy": [30, 40],
        "rsi_sell": [60, 70],
        "use_atr": [0, 1],
        "atr_period": [14],
        "atr_gate": [1.0],
        "sl_atr": [1.5, 2.5],
        "rr": [1.5, 2.5],
    }
    RANDOM_SPACE = {
        "use_trend": (0, 1, 1),
        "ma_slow": (30, 200, 5),
        "rsi_period": (5, 21, 1),
        "rsi_buy": (20, 45, 1),
        "rsi_sell": (55, 80, 1),
        "use_atr": (0, 1, 1),
        "atr_period": (7, 21, 1),
        "atr_gate": (0.6, 1.6, 0.05),
        "sl_atr": (1.0, 4.0, 0.1),
        "rr": (1.2, 4.0, 0.1),
    }

    @classmethod
    def valid_params(cls, params):
        return params["rsi_buy"] < 50 < params["rsi_sell"]

    @property
    def warmup(self) -> int:
        # 覆盖三个指标的最长需求(不随开关变 — 保证同参数任何组合行为一致)
        return max(self.params["ma_slow"],
                   self.params["rsi_period"] + 2,
                   self.params["atr_period"] * 3 + 1) + 2

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        p = self.params
        n = p["rsi_period"]
        rsi_now = _rsi(c, n)
        rsi_prev = _rsi(c[:-1], n)

        # 时机触发(核心, 常开): RSI 回穿 — 事件稀疏, 天然限频
        buy_trig = rsi_prev <= p["rsi_buy"] < rsi_now
        sell_trig = rsi_prev >= p["rsi_sell"] > rsi_now
        if not (buy_trig or sell_trig):
            return None

        # 趋势闸(可开关): 只顺慢线方向做
        if p["use_trend"]:
            ma = c[-p["ma_slow"]:].mean()
            if buy_trig and not c[-1] > ma:
                return None
            if sell_trig and not c[-1] < ma:
                return None

        # 环境闸(可开关): 当前波动 ≥ 常态波动 × atr_gate
        atr_now = _atr(h, l, c, p["atr_period"])
        if p["use_atr"]:
            atr_ref = _atr(h, l, c, p["atr_period"] * 3)
            if atr_ref <= 0 or atr_now < p["atr_gate"] * atr_ref:
                return None

        # 出场: ATR 自适应 SL/TP (行情死到 ATR=0 就不做)
        sl_d = p["sl_atr"] * atr_now
        if sl_d <= 0:
            return None
        tp_d = sl_d * p["rr"]
        price = float(c[-1])
        if buy_trig:
            return Signal("BUY", price - sl_d, price + tp_d)
        return Signal("SELL", price + sl_d, price - tp_d)
