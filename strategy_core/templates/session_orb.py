"""开盘区间突破模板(2026-08-15 译自 SessionORB_EA.mq5, 首个用 self.t 的时段类策略)

逻辑(全部时间 = 券商服务器时间, 与 self.t 同源):
  start_hour 起记录 range_bars 根 bar 的高低点 = 开盘区间
  → 之后 window_bars 根内, 首根收盘突破 区间高点+buffer → 买 / 跌破 区间低点-buffer → 卖
  → SL = 对面区间边界 ± sl_buffer, TP = rr × 风险距离
  → 每个 session 只做第一次突破(原 EA 的 MaxTradesPerSession=1)

与原 EA 的对应关系(这只 EA 本身写得干净, 几乎无损转换):
  · 单仓/服务端 SL/TP/收盘 bar 决策 — 与本框架纪律一字不差, 原样;
  · 区间精度零损失: 引擎 TF bar 的高低点就是桶内 M1 极值, 只要 range 时长对齐 bar 边界;
  · 风险百分比手数 = 钱管理不是入场逻辑, 舍弃(手数走 strategies.volume);
  · "每日 1 单"改为无状态实现: 只在本 session 窗口内【第一根】突破 bar 上开仓 —
    更早已有突破 bar(当时没空仓或已做过)= 错过不追, 与 EA 配额语义一致, runner 无状态安全;
  · 窗口跨午夜的配置按 EA 同款截断(EA 在新日 rebuild session, 等效只认当日窗口)。

判读预登记(2026-08-15): rr=2 → 含成本盈亏平衡胜率 ≈ 35%;
实测胜率减平衡线 > 2 个标准误才算有东西 — 与 daily_zone 结案用的同一把尺子。
"""
from typing import Optional

from ..base import Signal, Strategy


class SessionOrb(Strategy):
    PARAM_GRID = {
        "start_hour": [8, 9, 15, 16],   # session 起点(服务器时间; 伦敦/纽约两个开盘带)
        "range_bars": [2, 4],           # 开盘区间长度(bar 数; M15×2=30分钟, 原 EA 默认)
        "window_bars": [8],             # 区间收盘后允许交易的窗口(bar 数; M15×8=2小时)
        "buffer_points": [20],          # 突破确认缓冲(点)
        "sl_buffer_points": [30],       # SL 越过对面边界的缓冲(点)
        "rr": [1.5, 2.0, 3.0],          # TP = rr × 风险距离
    }
    RANDOM_SPACE = {
        "start_hour": (0, 23, 1),
        "range_bars": (1, 8, 1),
        "window_bars": (4, 24, 2),
        "buffer_points": (0, 100, 10),
        "sl_buffer_points": (0, 100, 10),
        "rr": (1.0, 4.0, 0.25),
    }

    @classmethod
    def valid_params(cls, params):
        return (0 <= params["start_hour"] <= 23 and params["range_bars"] >= 1
                and params["window_bars"] >= 1 and params["rr"] > 0
                and params["buffer_points"] >= 0 and params["sl_buffer_points"] >= 0)

    @property
    def warmup(self) -> int:
        # 只需覆盖"session 起点 → 当前"这段: 区间 + 窗口 + 少量余量。
        # 时段定位靠 self.t 的钟点, 不靠数根数 — 周末/缺口不会错位
        return self.params["range_bars"] + self.params["window_bars"] + 6

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        t = self.t
        if t is None or len(t) < 2:
            return None                      # 没有时间戳(老引擎/异常) → 不交易, 不猜
        ts = [int(x) for x in t]             # warmup ~16 根, 纯 python 足够快
        tf = min(b - a for a, b in zip(ts, ts[1:]) if b > a)   # bar 周期从时间戳自证

        now = ts[-1]                          # 当前(已收盘)bar 的开盘时刻
        day = now // 86400
        start = day * 86400 + self.params["start_hour"] * 3600
        range_end = start + self.params["range_bars"] * tf
        win_end = range_end + self.params["window_bars"] * tf

        # 当前 bar 必须是窗口 bar: 开在区间结束之后, 收在窗口截止之前(与 EA 的
        # "now <= tradingWindowEnd" 同口径 — now 即被评估 bar 的收盘时刻)
        if now < range_end or now + tf > win_end:
            return None

        # 开盘区间 = [start, range_end) 内 bar 的极值。TF bar 高低点即桶内 M1 极值,
        # 区间精度与原 EA 的 M1 计算零差异(前提: 时长对齐 bar 边界, 参数按 bar 数表达即天然对齐)
        rh = [float(h[i]) for i in range(len(ts)) if start <= ts[i] < range_end]
        rl = [float(l[i]) for i in range(len(ts)) if start <= ts[i] < range_end]
        if not rh:
            return None                       # 区间没有一根 bar(缺口/停盘) → 本 session 不做
        r_high, r_low = max(rh), min(rl)

        buf = self.params["buffer_points"] * self.point
        up, dn = r_high + buf, r_low - buf

        # 每 session 一单(无状态): 更早的窗口 bar 已有突破 → 那次才是"第一单", 不追。
        # 昨天的 bar 自动出局: 它们的开盘时刻必早于今天的 range_end
        for i in range(len(ts) - 1):
            if range_end <= ts[i] and ts[i] + tf <= win_end \
                    and (float(c[i]) > up or float(c[i]) < dn):
                return None

        cc = float(c[-1])
        slb = self.params["sl_buffer_points"] * self.point
        if cc > up:                           # 首根收盘突破上沿 → 买
            sl = r_low - slb
            return Signal("BUY", sl, cc + self.params["rr"] * (cc - sl))
        if cc < dn:                           # 首根收盘跌破下沿 → 卖
            sl = r_high + slb
            return Signal("SELL", sl, cc - self.params["rr"] * (sl - cc))
        return None
