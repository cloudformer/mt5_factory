"""self.t 约定回归(2026-08-15): 引擎在每次 on_bar 前把与 o/h/l/c 对齐的 bar 时间戳
挂在 strat.t 上(epoch 秒, 券商服务器时间)。时段类策略(session_orb 等)靠它取钟点。

为什么要有这张网: 这是引擎与策略之间的"第二条数据通道", 改引擎切片逻辑时最容易
悄悄弄断它(比如窗口下标改了但 t 的切片没跟上 → 时段策略全体静默错位)。
runner 侧同一约定(rates["time"]), 无法在此测试, 靠 Windows 上的部署验证。
"""
import numpy as np

import strategy_core
from src.services.backtest import run_backtest
from strategy_core.base import Strategy


class _Probe(Strategy):
    """探针: 记录每次 on_bar 收到的 self.t, 校验对齐/递增"""
    calls: list = []

    @property
    def warmup(self):
        return 5

    def on_bar(self, o, h, l, c):
        assert self.t is not None, "t 没挂上"
        assert len(self.t) == len(c), "t 与 c 不对齐"
        tt = list(self.t)
        assert all(tt[i] < tt[i + 1] for i in range(len(tt) - 1)), "t 不递增"
        _Probe.calls.append(int(tt[-1]))
        return None


def _m1_three_days():
    """3 天 × 8 小时的 M1, 故意缺分钟(缺分钟属正常, 按时间桶聚合)"""
    t0 = 1700000000 - 1700000000 % 900
    times = [t0 + d * 86400 + m * 60
             for d in range(3) for m in range(480) if m % 7]
    n = len(times)
    px = np.full(n, 1.10)
    return {"time": np.array(times, np.int64), "open": px,
            "high": px + 0.001, "low": px - 0.001, "close": px,
            "spread": np.full(n, 10, np.int64)}


def test_engine_attaches_aligned_bar_times():
    _Probe.calls = []
    strategy_core.TEMPLATES["_probe"] = _Probe
    try:
        run_backtest(_m1_three_days(), "_probe", {}, 0.0001, "M15", 0.01)
    finally:
        del strategy_core.TEMPLATES["_probe"]
    assert len(_Probe.calls) > 50          # 确实逐 bar 调了
    # 钟点可提取(时段策略的用法): 所有值都在 0~23
    hours = {(ts % 86400) // 3600 for ts in _Probe.calls}
    assert hours <= set(range(24))


def test_existing_templates_untouched():
    """不读 t 的模板行为不变: 基类默认 t=None, 老模板从不访问它"""
    assert Strategy.t is None
