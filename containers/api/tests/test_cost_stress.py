"""成本敏感性回归(2026-08-17): 压力测试的物理 — 成本升净点必降(单调性),
点差数组等比放大与固定点差参数两条路都验。改撮合成本路径前后必跑。"""
import numpy as np

from src.services.backtest import run_backtest

P = {"n_break": 20, "n_filter": 100, "atr_n": 14, "k_sl": 2.0, "rr": 3.0,
     "buf_atr": 0.25, "vol_lo": 0.5, "vol_hi": 3.0}


def _m1():
    """test_fable 同款: 300 根震荡 + 100 根单边涨 → 至少 1 笔交易"""
    t, c = [], []
    t0 = 1700000000 - 1700000000 % 3600
    for k in range(400):
        lvl = 100.0 + (0.2 if k % 2 else -0.2) if k < 300 else 100.0 + 1.0 * (k - 299)
        for m in range(60):
            t.append(t0 + k * 3600 + m * 60)
            c.append(lvl + (0.02 if m % 2 else -0.02))
    c = np.array(c)
    return {"time": np.array(t, np.int64), "open": c.copy(), "high": c + 0.03,
            "low": c - 0.03, "close": c, "spread": np.full(len(t), 10, np.int64)}


def _net(m1, **costs):
    res = run_backtest(m1, "fable", P, 0.01, "H1", oos_split=None, **costs)
    assert res["metrics"]["trades"] > 0
    return res["metrics"]["net_points"]


def test_commission_and_slippage_monotone():
    base = _net(_m1(), slippage_points=3, commission_points=7)
    x2 = _net(_m1(), slippage_points=6, commission_points=14)
    assert x2 < base                        # 成本翻倍 → 净点必降


def test_recorded_spread_scaling_monotone():
    m1 = _m1()
    base = _net(m1, slippage_points=3, commission_points=7, spread_points=None)
    m1x = {**m1, "spread": m1["spread"] * 2}     # 端点同款: 逐bar记录点差等比放大
    x2 = _net(m1x, slippage_points=3, commission_points=7, spread_points=None)
    assert x2 < base


def test_fixed_spread_param_monotone():
    base = _net(_m1(), slippage_points=3, commission_points=7, spread_points=10)
    x2 = _net(_m1(), slippage_points=3, commission_points=7, spread_points=20)
    assert x2 < base
