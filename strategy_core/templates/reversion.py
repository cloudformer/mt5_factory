"""REVERSION(回头浪): 极端回撤后的反转, 与 FABLE 互补的另一半 regime 拼图
(2026-08-16 原创设计, Claude Fable 5 × Frank; 长线周期 H4/D1 优先)

思想: FABLE 在"长短同向"格收割趋势右尾, 在"长短打架"格交期权费;
REVERSION 反过来 —— 专吃短线过度反应的回摆, 目标正是那两个打架格。
一格一个专家, regime 门各锁各的, 这是为门定制的第二块拼图。

两个信号(参数 signal, 分开生成分开判):
  signal=1 顺势接刀: 长线同向 + 短线 n_dip 根内暴跌/暴涨 ≥ d_atr×ATR + 【第一根】
           止跌(涨)确认收盘 → 顺长线方向进(牛市买急跌/熊市卖急弹)。
           均值回归家族里证据最硬的形态(Connors RSI-2 一族的骨架)。
  signal=2 逆势抄底/摸顶: 长线逆向(真接飞刀) + 深跌(涨)后吞没级反转 bar
           (收过前根极值)才进。Frank 点名的"大型反转"。
共同结构: 确认后入场, SL 压在极值外 sl_buf_atr×ATR, TP = rr×风险(rr 1~2, 高胜率型);
只做首根确认 bar(一次性, runner 无状态安全); 现价须仍在回撤区间的近端半区(不追已弹高的)。

判读预登记(2026-08-16, 签字画押):
  · rr=1 → 平衡胜率 50%, rr=2 → 33.3%; 实测超平衡线 >2 SE 才算有东西;
  · 画像验收: signal=1 应在 ABA/ABB(长↑短↓)及镜像 BAx 显著为正 — 与 FABLE 互补;
    signal=2 先验最怀疑(接飞刀=负期望之王), 若有肉只应在高波格(xxA);
    证伪即删, 不调参救 — 尤其 signal=2。
  · 不保证盈利; 保证单笔损失有界 + 假设可证伪。
"""
from typing import Optional

import numpy as np

from ..base import Signal, Strategy


class Reversion(Strategy):
    PARAM_GRID = {
        "signal": [1, 2],
        "n_filter": [200],        # 长线过滤(SMA)
        "n_dip": [5, 10],         # 短线极端回看(bar 数)
        "d_atr": [3.0, 5.0],      # 回撤深度门槛(ATR 倍数)
        "atr_n": [14],
        "sl_buf_atr": [0.5],      # SL 越过极值的缓冲(ATR 倍数)
        "rr": [1.0, 2.0],         # TP = rr×风险(均值回归: 目标近, 胜率高)
    }
    RANDOM_SPACE = {
        "signal": (1, 2, 1),
        "n_filter": (50, 400, 25),
        "n_dip": (3, 30, 1),
        "d_atr": (1.5, 8.0, 0.25),
        "atr_n": (7, 28, 1),
        "sl_buf_atr": (0.0, 2.0, 0.25),
        "rr": (0.5, 3.0, 0.25),
    }

    @classmethod
    def valid_params(cls, params):
        return (params["signal"] in (1, 2) and params["n_dip"] >= 2
                and params["n_filter"] >= params["n_dip"] and params["d_atr"] > 0
                and params["atr_n"] >= 2 and params["sl_buf_atr"] >= 0
                and params["rr"] > 0)

    @property
    def warmup(self) -> int:
        p = self.params
        return p["n_filter"] + p["atr_n"] + p["n_dip"] + 5

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        p = self.params
        n_d, n_a = int(p["n_dip"]), int(p["atr_n"])
        cc, pc, ppc = float(c[-1]), float(c[-2]), float(c[-3])

        tr = np.maximum(h[1:] - l[1:],
                        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        atr = float(tr[-n_a:].mean())
        if atr <= 0:
            return None

        sma = float(c[-int(p["n_filter"]):].mean())
        hi = float(h[-n_d:].max())
        lo = float(l[-n_d:].min())
        depth = hi - lo
        if depth < p["d_atr"] * atr:
            return None                      # 没有极端回撤, 无浪可回

        sig, buf = int(p["signal"]), p["sl_buf_atr"] * atr
        up_ok = cc > sma                     # 长线向上
        # 买向: 短线深跌后确认。一次性 = 前一根还在跌(首根确认才做, 弹了两根不追);
        # 且现价仍在回撤区间的下半区(离坑底近, 风险距离才有意义)
        first_up = cc > pc and pc <= ppc and cc <= lo + depth / 2
        first_dn = cc < pc and pc >= ppc and cc >= hi - depth / 2
        if sig == 1:
            buy = up_ok and first_up                       # 牛市买急跌
            sell = (not up_ok) and first_dn                # 熊市卖急弹
        else:
            # 逆势大反转: 确认升级为吞没级(收过前根极值), 长线逆向才叫"抄底/摸顶"
            buy = (not up_ok) and first_up and cc > float(h[-2])
            sell = up_ok and first_dn and cc < float(l[-2])

        if buy:
            sl = lo - buf
            return Signal("BUY", sl, cc + p["rr"] * (cc - sl))
        if sell:
            sl = hi + buf
            return Signal("SELL", sl, cc - p["rr"] * (sl - cc))
        return None
