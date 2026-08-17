"""PULLBACK(顺势低吸): FABLE 的便宜入场变体, 占 regime 短趋势维的回调格
(2026-08-16 原创设计, Claude Fable 5 × Frank)

与 regime 的同构关系: 长线过滤(SMA+中线) ≅ 长趋势维; "从高点回撤"触发 ≅ 短趋势维
翻到逆向 — 入场日应落在 ABA/ABB(长↑短↓)及镜像, 正是 FABLE 追突破买不到的便宜日。
与已证伪的 reversion.signal1 的本质区别在【出场】: 那个用 rr1~2 快出场(均值回归口径,
20 年数据判了死刑); 本模板低吸后接 FABLE 同款凸性出场(rr=3~4 骑趋势) —
黄金 20 年的实证是"快出场死, 放右尾活", 同样的便宜入场换凸性出场 = 全新假设。

规则(全部收盘 bar 决策, 纯函数无状态):
  过滤  多: 收盘 > SMA(n_filter) 且 > n_filter 通道中线; 空镜像(与 fable 同款)
  回调  近 n_high 根的极值 到 近 n_pull 根的反向极值, 落差 ≥ retrace_atr×ATR
  恢复  收盘首次收复前根极值(cc > h[-2], 前根未收复 — 首根确认, 不接飞刀不追第二根)
  低吸  现价仍低于趋势高点(cc < hh) — 回到高点就是突破, 那是 FABLE 的地盘
  出场  SL = 回调低点 − sl_buf_atr×ATR(结构止损), TP = rr×风险(凸性)

判读预登记(2026-08-16, 签字画押):
  · rr=3 → 平衡胜率 25%, rr=4 → 20%; 超平衡线 >2 SE 才算有东西;
  · 画像验收: 入场日应集中在长短背离格(ABx/BAx), 利润应与 FABLE 同宿(骑进 AAx/BBx);
  · 品种预期: XAUUSD 应活(趋势品种), EURUSD 大概率死 — 死了是品种性格的第三次确认;
  · 证伪即删不调参救; 不保证盈利, 保证单笔损失有界 + 可证伪。
建议周期 H1/H4, 品种 XAUUSD(+EURUSD 作对照)。
"""
from typing import Optional

import numpy as np

from ..base import Signal, Strategy


class Pullback(Strategy):
    PARAM_GRID = {
        "n_filter": [200],        # 长线结构过滤(与 fable 同款)
        "n_high": [20],           # 趋势极值回看
        "n_pull": [5, 10],        # 回调极值回看
        "retrace_atr": [1.5, 2.5],  # 回调深度门槛(ATR 倍数)
        "atr_n": [14],
        "sl_buf_atr": [0.5],      # SL 越过回调极值的缓冲
        "rr": [3.0, 4.0],         # 凸性出场(FABLE 同款)
    }
    RANDOM_SPACE = {
        "n_filter": (50, 400, 25),
        "n_high": (10, 60, 5),
        "n_pull": (3, 20, 1),
        "retrace_atr": (0.5, 5.0, 0.25),
        "atr_n": (7, 28, 1),
        "sl_buf_atr": (0.0, 2.0, 0.25),
        "rr": (1.5, 6.0, 0.25),
    }

    @classmethod
    def valid_params(cls, params):
        return (params["n_filter"] >= params["n_high"] >= params["n_pull"] >= 2
                and params["retrace_atr"] > 0 and params["atr_n"] >= 2
                and params["sl_buf_atr"] >= 0 and params["rr"] > 0)

    @property
    def warmup(self) -> int:
        p = self.params
        return p["n_filter"] + p["atr_n"] + 5

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        p = self.params
        n_h, n_p, n_a = int(p["n_high"]), int(p["n_pull"]), int(p["atr_n"])
        cc, pc = float(c[-1]), float(c[-2])

        tr = np.maximum(h[1:] - l[1:],
                        np.abs(np.stack([h[1:] - c[:-1], l[1:] - c[:-1]])).max(axis=0))
        atr = float(tr[-n_a:].mean())
        if atr <= 0:
            return None

        sma = float(c[-int(p["n_filter"]):].mean())
        mid = (float(h[-int(p["n_filter"]):].max())
               + float(l[-int(p["n_filter"]):].min())) / 2.0
        need = p["retrace_atr"] * atr
        buf = p["sl_buf_atr"] * atr

        if cc > sma and cc > mid:                      # 多头资格(长线向上)
            hh = float(h[-(n_h + 1):-1].max())         # 趋势高点(不含当前bar —
            dip = float(l[-n_p:].min())                # 否则"不追高"守卫形同虚设)
            # 回调够深 + 首根收复前根最高(前根没收复过) + 仍低于高点(低吸不追高)
            if (hh - dip >= need and cc > float(h[-2]) and not pc > float(h[-3])
                    and cc < hh):
                sl = dip - buf
                return Signal("BUY", sl, cc + p["rr"] * (cc - sl))
        elif cc < sma and cc < mid:                    # 空头镜像(长线向下, 卖反弹)
            ll = float(l[-(n_h + 1):-1].min())
            rip = float(h[-n_p:].max())
            if (rip - ll >= need and cc < float(l[-2]) and not pc < float(l[-3])
                    and cc > ll):
                sl = rip + buf
                return Signal("SELL", sl, cc - p["rr"] * (sl - cc))
        return None
