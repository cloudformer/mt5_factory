"""唐奇安通道突破模板"""
from typing import Optional

from ..base import Signal, Strategy


class Breakout(Strategy):
    PARAM_GRID = {
        "channel": [20, 55, 100],
        "sl_points": [200, 400, 800],
        "rr": [1.5, 2.0, 3.0],
    }
    RANDOM_SPACE = {
        "channel": (10, 200, 5),
        "sl_points": (100, 1500, 50),
        "rr": (1.2, 4.0, 0.1),
    }

    @property
    def warmup(self) -> int:
        return self.params["channel"] + 2

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        n = self.params["channel"]
        hh = h[-n - 1:-1].max()  # 不含当前bar的通道上沿
        ll = l[-n - 1:-1].min()

        sl_d = self.params["sl_points"] * self.point
        tp_d = sl_d * self.params["rr"]
        price = float(c[-1])

        if price > hh:
            return Signal("BUY", price - sl_d, price + tp_d)
        if price < ll:
            return Signal("SELL", price + sl_d, price - tp_d)
        return None
