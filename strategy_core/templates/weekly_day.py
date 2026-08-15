"""星期效应模板(2026-08-15 译自 003-Weekly-Day-Reversal.mq5, 第二只用 self.t 的策略)

逻辑(全部时间 = 券商服务器时间):
  新一天的第一根 bar 收盘时, 看刚结束的上一交易日是不是 check_day(如周一)
  → 是则按其阴阳定方向: direct=顺着做 / reverse=反着做
  → 可选 ATR 过滤: check_day 当日振幅 < atr_min × 日ATR → 不做
  → SL = k_atr × 日ATR, TP = rr × SL(日ATR = 近 atr_period 个交易日 TR 简单均值,
    与 MT5 iATR 同口径, 截止到 check_day 当日)

与原 EA 的声明差异(为一只研究 EA 不动尺子, 2026-08-15):
  1. CloseHour 强平不做 —— 引擎/runner 离场只有服务端 SL/TP, 没有时间出场通道。
     后果: 持仓可跨日, 测的是"星期方向 + ATR 定距 SL/TP", 不是纯日内星期效应;
     kDailyATR=0(无 SL 裸测)同理不支持 —— 每单必带 SL 是铁律;
  2. 收盘 bar 决策: 判定在新日首根 bar 收盘, 入场在下一根 bar 开盘(EA 是新日首 tick,
     M15 下晚 15~30 分钟);
  3. 一次性语义: 只在新日【第一根】bar 上判定, 当时不空仓 = 错过不追
     (EA 被持仓挡住会在当日稍后重试), runner 无状态安全;
  4. "上一交易日" = 窗口里当前天之前最近的有 bar 的日历天。有周日碎 bar 的券商下
     周日算独立一天(check_day=1 周一开盘的"前一日"会是周日) —— 当前券商 EET 无周日 bar;
  5. 风险百分比手数 = 钱管理不是入场逻辑, 舍弃(手数走 strategies.volume)。

判读预登记(2026-08-15): 星期效应是日历异象里最经典的"挖出来的规律", 先验怀疑;
rr=2.5 → 含成本盈亏平衡胜率 ≈ 28.6%, 实测胜率减平衡线 > 2 个标准误才算有东西。
另: 同日 direct 与 reverse 是近镜像(SL/TP 不对称所以不严格), 一正一负不构成双重证据。
"""
from typing import Optional

import numpy as np

from ..base import Signal, Strategy


class WeeklyDay(Strategy):
    PARAM_GRID = {
        "check_day": [1, 2, 3, 4, 5],   # MT5 口径: 0=周日..6=周六
        "direction": [0, 1],            # 0=顺日方向 / 1=反日方向(EA 默认 reverse)
        "atr_on": [0, 1],               # 振幅过滤: check_day 当日振幅须 ≥ atr_min × 日ATR
        "atr_min": [1.5],
        "atr_period": [14],             # 日 ATR 回看交易日数
        "k_atr": [0.5],                 # SL = k_atr × 日ATR
        "rr": [2.5],                    # TP = rr × SL
        "day_bars": [96],               # 一天的 bar 数上限(M15=96), 只用于 warmup 定长
    }
    RANDOM_SPACE = {
        "check_day": (1, 5, 1),
        "direction": (0, 1, 1),
        "atr_on": (0, 1, 1),
        "atr_min": (0.5, 3.0, 0.25),
        "atr_period": (5, 28, 1),
        "k_atr": (0.2, 2.0, 0.1),
        "rr": (0.5, 4.0, 0.25),
        "day_bars": (24, 288, 24),
    }

    @classmethod
    def valid_params(cls, params):
        return (0 <= params["check_day"] <= 6 and params["direction"] in (0, 1)
                and params["atr_period"] >= 2 and params["atr_min"] > 0
                and params["k_atr"] > 0 and params["rr"] > 0
                and params["day_bars"] >= 20)

    @property
    def warmup(self) -> int:
        # 需覆盖 atr_period+1 个交易日(TR 要前收) + 余量: 窗口首日可能是被切断的半天,
        # 缺分钟又使每日实际 bar 数 < day_bars → 余量 +3 天足以把半天挤出 ATR 切片
        return (self.params["atr_period"] + 3) * self.params["day_bars"] + 5

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        t = self.t
        if t is None or len(t) < 2:
            return None                      # 没有时间戳(老引擎/异常) → 不交易, 不猜
        days = np.asarray(t, np.int64) // 86400
        cur = int(days[-1])
        if int(days[-2]) == cur:
            return None                      # 只在新日第一根 bar 上判定(一次性语义)

        prev_mask = days < cur
        if not prev_mask.any():
            return None
        prev = int(days[prev_mask][-1])      # 上一交易日(最近有 bar 的天)
        if (prev + 4) % 7 != self.params["check_day"]:   # epoch 日0=周四 → +4 对齐 MT5 口径
            return None

        # 上一交易日 OHLC + 近 atr_period 个交易日的日线序列(算 TR 要前收)
        uds = np.unique(days[days <= prev])
        n = int(self.params["atr_period"])
        if len(uds) < n + 1:
            return None                      # 历史不足: EA OnInit 同款直接不做
        uds = uds[-(n + 1):]
        dh, dl, dc = [], [], []
        for d_ in uds:
            m = days == d_
            dh.append(float(h[m].max()))
            dl.append(float(l[m].min()))
            dc.append(float(c[m][-1]))
        trs = [max(dh[k] - dl[k], abs(dh[k] - dc[k - 1]), abs(dl[k] - dc[k - 1]))
               for k in range(1, len(uds))]
        atr = sum(trs) / len(trs)            # TR 简单均值 = MT5 iATR 同口径
        if atr <= 0:
            return None

        m = days == prev
        d_open, d_close = float(o[m][0]), float(c[m][-1])
        if self.params["atr_on"] and (dh[-1] - dl[-1]) < self.params["atr_min"] * atr:
            return None                      # check_day 当日振幅太小 → 无效应可反/可顺

        bullish = d_close > d_open
        up = bullish if self.params["direction"] == 0 else not bullish
        cc = float(c[-1])
        sl_d = self.params["k_atr"] * atr
        if up:
            return Signal("BUY", cc - sl_d, cc + self.params["rr"] * sl_d)
        return Signal("SELL", cc + sl_d, cc - self.params["rr"] * sl_d)
