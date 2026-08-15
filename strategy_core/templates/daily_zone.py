"""日区间带模板(2026-08-14 译自 DailyZoneRecovery.mq5 的三个入场信号, 裸信号版)

原 EA = 三个入场逻辑 + 网格摊平("Recovery")的篮子出场。本模板只取入场逻辑:
网格/篮子与系统单仓模型和"每单必带服务端 SL/TP"铁律结构性不兼容 —— 而且按尺子哲学
本来就该先测裸信号有没有 edge(网格的本质是把差入场的亏损摊平推迟, 裸信号亏 = 直接删)。

三个信号围着"前一天高低点各画一条带(zone)":
  signal=1 突破回踩: 价格曾突破带外 distance% 又回到带内 → 顺突破方向进(顺势接力)
  signal=2 穿越全程: 窗口内先碰过低带、后涨破高带+distance% → 买(镜像卖) —— 全幅动量
  signal=3 碰-离-再碰: 碰带 → 离开 distance% → 又回带内 → 进(低带双底买/高带双顶卖, 逆势)

与 EA 的三处已声明差异(不动引擎的代价, 2026-08-14 Frank 定"这就是一个策略"):
  1. on_bar 没有时间戳 → "昨日"用滑动的 day_bars 根近似(M15 填 96, H1 填 24, 生成时
     与周期一起选); 参考区间 = 窗口里 [-2D:-D] 那一块("前一个日长块"), 不再对齐日历日;
  2. S2/S3 原是逐日重置的状态机 → 改为"最近 D 根窗口内重放序列, 只在完成信号的那根 bar
     开仓"(一次性语义保留), 纯函数无实例状态 —— 天生满足 runner 无状态铁律;
  3. 双向同时触发(EA 的 sig==0 会两边各开一篮) → 单仓模型下按信号冲突不开仓(保守)。
出场 = 服务端 SL/TP(入场价的百分比换算绝对价); EA 的篮子均价出场随网格一起不做。
ATR 过滤照搬(其实是"前一块振幅 相对 前 N 块平均振幅"的百分比窗, 不是真 ATR)。
"""
from typing import Optional

from ..base import Signal, Strategy


class DailyZone(Strategy):
    PARAM_GRID = {
        "signal": [1, 2, 3],
        "day_bars": [96],              # 一"天"的 bar 数(M15=96; 换周期要一起换)
        "zone_pct": [0.13, 0.28],      # 带宽(%; EA 口径: 按参考高点算, 高低两条带同宽)
        "dist_pct": [0.25, 0.4],       # 突破/回落距离(%)
        "tp_pct": [0.15, 0.5],         # 止盈(入场价的 %)
        "sl_pct": [1.5],               # 止损(入场价的 %)
        "atr_on": [1],                 # 振幅过滤开关
        "atr_period": [4],             # 过滤回看几"天"(块)
        "atr_min_pct": [25.0],         # 前一块振幅下限(% of 均值)
        "atr_max_pct": [150.0],        # 上限
    }
    RANDOM_SPACE = {
        "signal": (1, 3, 1),
        "day_bars": (24, 288, 24),
        "zone_pct": (0.05, 0.6, 0.01),
        "dist_pct": (0.1, 1.0, 0.05),
        "tp_pct": (0.05, 1.5, 0.05),
        "sl_pct": (0.5, 3.0, 0.1),
        "atr_on": (0, 1, 1),
        "atr_period": (2, 8, 1),
        "atr_min_pct": (10, 60, 5),
        "atr_max_pct": (100, 250, 10),
    }

    @classmethod
    def valid_params(cls, params):
        return (params["day_bars"] >= 20 and params["zone_pct"] > 0
                and params["dist_pct"] > 0 and params["tp_pct"] > 0
                and params["sl_pct"] > 0
                and params["atr_min_pct"] < params["atr_max_pct"])

    @property
    def warmup(self) -> int:
        d = self.params["day_bars"]
        blocks = (self.params["atr_period"] + 1) if self.params["atr_on"] else 2
        return max(blocks, 2) * d + 5

    # ---------- 振幅过滤(EA 的 "ATR filter"): 前一块振幅须在前 N 块均值的百分比窗内 ----------
    def _range_ok(self, h, l) -> bool:
        if not self.params["atr_on"]:
            return True
        d, n = self.params["day_bars"], max(int(self.params["atr_period"]), 2)
        ranges = []
        for k in range(1, n + 1):                   # 块 k = 窗口里第 k 个"日长块"(近→远)
            hh, ll = h[-(k + 1) * d:-k * d], l[-(k + 1) * d:-k * d]
            if len(hh) < d:
                return True                          # 数据不足: EA 同款放行(过滤器失效不拦)
            ranges.append(float(hh.max() - ll.min()))
        avg = sum(ranges) / n
        if avg <= 0:
            return True
        pct = ranges[0] / avg * 100.0
        return self.params["atr_min_pct"] <= pct <= self.params["atr_max_pct"]

    # ---------- 三个信号: 返回 +1 买 / -1 卖 / 0 冲突 / None 无 ----------
    def _sig1(self, h, l, c, zones) -> Optional[int]:
        """突破回踩: 现价在带内, 且往回看最近一次离带是在突破侧、期间摸到过 thr"""
        h_top, h_bot, l_top, l_bot = zones
        d = self.params["day_bars"]
        cc = float(c[-1])
        in_high = h_bot <= cc <= h_top
        in_low = l_bot <= cc <= l_top
        if not in_high and not in_low:
            return None
        sc, sh, sl_ = c[-(d + 1):-1], h[-(d + 1):-1], l[-(d + 1):-1]   # 当前 bar 之前的一"天"
        buy = sell = False
        if in_high:
            thr = h_top * (1.0 + self.params["dist_pct"] / 100.0)
            out = (sc < h_bot) | (sc > h_top)
            if out.any():
                last = len(sc) - 1 - int(out[::-1].argmax())   # 最近一次离带
                if sc[last] > h_top and (sh[last:] >= thr).any():
                    buy = True
        if in_low:
            thr = l_bot * (1.0 - self.params["dist_pct"] / 100.0)
            out = (sc < l_bot) | (sc > l_top)
            if out.any():
                last = len(sc) - 1 - int(out[::-1].argmax())
                if sc[last] < l_bot and (sl_[last:] <= thr).any():
                    sell = True
        if buy and sell:
            return 0
        return 1 if buy else (-1 if sell else None)

    def _sig2(self, c, zones) -> Optional[int]:
        """穿越全程(窗口重放): 先碰低带后破高带+dist → 买; 只认完成在当前 bar 的信号
        (一次性语义: 完成在更早 bar 的序列 = 当时没空仓就错过, 不追)"""
        h_top, h_bot, l_top, l_bot = zones
        d, dist = self.params["day_bars"], self.params["dist_pct"] / 100.0
        seg = c[-d:]
        last = len(seg) - 1

        def chase(m1, m2):
            """m1 先真、其后 m2 首真的下标(无则 None)"""
            if not m1.any():
                return None
            i1 = int(m1.argmax())
            m2b = m2[i1:]
            return i1 + int(m2b.argmax()) if m2b.any() else None

        buy_at = chase(seg <= l_top, seg >= h_top * (1.0 + dist))
        sell_at = chase(seg >= h_bot, seg <= l_bot * (1.0 - dist))
        buy, sell = buy_at == last, sell_at == last
        if buy and sell:
            return 0
        return 1 if buy else (-1 if sell else None)

    def _sig3(self, c, zones) -> Optional[int]:
        """碰-离-再碰(窗口重放三段): 高带侧 = 碰 hBot → 跌离 dist → 回到带内 → 卖;
        低带侧镜像买。只认完成在当前 bar 的信号"""
        h_top, h_bot, l_top, l_bot = zones
        d, dist = self.params["day_bars"], self.params["dist_pct"] / 100.0
        seg = c[-d:]
        last = len(seg) - 1

        def chase3(m1, m2, m3):
            if not m1.any():
                return None
            i1 = int(m1.argmax())
            m2b = m2[i1:]
            if not m2b.any():
                return None
            i2 = i1 + int(m2b.argmax())
            m3b = m3[i2:]
            return i2 + int(m3b.argmax()) if m3b.any() else None

        sell_at = chase3(seg >= h_bot, seg <= h_bot * (1.0 - dist),
                         (seg >= h_bot) & (seg <= h_top))
        buy_at = chase3(seg <= l_top, seg >= l_top * (1.0 + dist),
                        (seg >= l_bot) & (seg <= l_top))
        buy, sell = buy_at == last, sell_at == last
        if buy and sell:
            return 0
        return 1 if buy else (-1 if sell else None)

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        d = self.params["day_bars"]
        if not self._range_ok(h, l):
            return None
        # 参考区间 = 前一个"日长块"([-2D:-D]): EA 用昨日 D1 高低点, 无时间戳下的滑动近似
        rh, rl = h[-2 * d:-d], l[-2 * d:-d]
        if len(rh) < d:
            return None
        d_high, d_low = float(rh.max()), float(rl.min())
        zone = d_high * self.params["zone_pct"] / 100.0 / 2.0   # EA 口径: 两条带同用高点定宽
        zones = (d_high + zone, d_high - zone, d_low + zone, d_low - zone)

        sig = {1: lambda: self._sig1(h, l, c, zones),
               2: lambda: self._sig2(c, zones),
               3: lambda: self._sig3(c, zones)}[int(self.params["signal"])]()
        if sig is None or sig == 0:     # 0 = 双向同触发: 单仓模型按冲突不开仓(保守)
            return None

        px = float(c[-1])
        sl_d = px * self.params["sl_pct"] / 100.0
        tp_d = px * self.params["tp_pct"] / 100.0
        if sig > 0:
            return Signal("BUY", px - sl_d, px + tp_d)
        return Signal("SELL", px + sl_d, px - tp_d)
