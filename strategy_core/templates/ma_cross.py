"""双均线交叉模板"""
from typing import Optional

from ..base import Signal, Strategy


class MaCross(Strategy):
    PARAM_GRID = {
        "fast": [5, 10, 20],
        "slow": [50, 100, 200],
        "sl_points": [200, 400, 800],
        "rr": [1.5, 2.0, 3.0],  # tp = sl * rr
    }

    @classmethod
    def valid_params(cls, params):
        return params["fast"] < params["slow"]

    @property
    def warmup(self) -> int:
        return self.params["slow"] + 2

    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        fast, slow = self.params["fast"], self.params["slow"]
        f_now, f_prev = c[-fast:].mean(), c[-fast - 1:-1].mean()
        s_now, s_prev = c[-slow:].mean(), c[-slow - 1:-1].mean()

        sl_d = self.params["sl_points"] * self.point
        tp_d = sl_d * self.params["rr"]
        price = float(c[-1])

        if f_prev <= s_prev and f_now > s_now:
            return Signal("BUY", price - sl_d, price + tp_d)
        if f_prev >= s_prev and f_now < s_now:
            return Signal("SELL", price + sl_d, price - tp_d)
        return None
