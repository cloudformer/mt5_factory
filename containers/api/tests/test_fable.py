"""fable 模板回归(2026-08-15): 突破买/结构过滤拦截/一次性/波动窗 的已知答案 +
真引擎端到端。改模板前后必跑(与 test_engine 同一张网)。

构造口径: n_filter=100, n_break=20, atr_n=14 → warmup=120;
震荡段 = 100±0.2 锯齿(h=c+0.1, l=c-0.1), 突破 = 末根跳到 103。
"""
import numpy as np
import pytest

from src.services.backtest import run_backtest
from strategy_core.templates.fable import Fable

P = {"n_break": 20, "n_filter": 100, "atr_n": 14, "k_sl": 2.0, "rr": 3.0,
     "buf_atr": 0.25, "vol_lo": 0.5, "vol_hi": 3.0}


def mk(c_arr, **over):
    strat = Fable({**P, **over}, 0.01)
    c = np.asarray(c_arr, float)
    return strat, c.copy(), c + 0.1, c - 0.1, c


def zigzag(n, last=None):
    """100±0.2 锯齿 n 根; last 覆盖末根(若给)"""
    c = np.array([100.0 + (0.2 if i % 2 else -0.2) for i in range(n)])
    if last is not None:
        c[-1] = last
    return c


def ref_atr(h, l, c, n=14):
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(tr[-n:].mean())


def test_breakout_buy_with_exact_sl_tp():
    strat, o, h, l, c = mk(zigzag(125, last=103.0))
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    atr = ref_atr(h, l, c)
    assert sig.sl == pytest.approx(103.0 - 2.0 * atr)
    assert sig.tp == pytest.approx(103.0 + 3.0 * 2.0 * atr)


def test_no_breakout_no_signal():
    strat, o, h, l, c = mk(zigzag(125))          # 一直在通道里 → 无信号
    assert strat.on_bar(o, h, l, c) is None


def test_structure_filter_blocks_countertrend():
    # 单边下跌里向上跳破 20 根高点, 但收盘仍在 SMA/中线之下 → 多头无资格, 空头无下破 → None
    c = np.array([110.0 - 0.1 * i for i in range(125)])
    c[-1] = c[-2] + 3.0
    strat, o, h, l, c = mk(c)
    assert strat.on_bar(o, h, l, c) is None


def test_one_shot_first_bar_only():
    # 前一根已破通道(103), 当前继续新高(103.5) → 首根语义: 不追
    c = zigzag(125, last=103.5)
    c[-2] = 103.0
    strat, o, h, l, c = mk(c)
    assert strat.on_bar(o, h, l, c) is None


def test_vol_window_blocks_both_sides():
    c = zigzag(125, last=103.0)                  # 同突破构造, ratio≈1.4
    strat, o, h, l, cl = mk(c, vol_hi=1.0)       # 上限压到 1.0 → 视作乱纪元
    assert strat.on_bar(o, h, l, cl) is None
    strat, o, h, l, cl = mk(c, vol_lo=2.0)       # 下限抬到 2.0 → 视作死市
    assert strat.on_bar(o, h, l, cl) is None


def test_valid_params():
    assert Fable.valid_params(P)
    assert not Fable.valid_params({**P, "n_filter": 10})   # 过滤窗短于突破窗
    assert not Fable.valid_params({**P, "vol_lo": 3.0})    # 窗上下颠倒
    assert not Fable.valid_params({**P, "rr": 0})


# ---------- 真引擎端到端: 震荡 300 根 H1 → 单边上涨, 只该有 BUY ----------

def _m1_flat_then_rise():
    t, c = [], []
    t0 = 1700000000 - 1700000000 % 3600
    for k in range(400):
        lvl = 100.0 + (0.2 if k % 2 else -0.2) if k < 300 else 100.0 + 1.0 * (k - 299)
        for m in range(60):
            t.append(t0 + k * 3600 + m * 60)
            c.append(lvl + (0.02 if m % 2 else -0.02))
    c = np.array(c)
    n = len(t)
    return {"time": np.array(t, np.int64), "open": c.copy(), "high": c + 0.03,
            "low": c - 0.03, "close": c, "spread": np.full(n, 10, np.int64)}


def test_engine_end_to_end_only_buys_in_uptrend():
    res = run_backtest(_m1_flat_then_rise(), "fable", P, 0.01, "H1", oos_split=None)
    trades = res["trades"]
    assert trades, "上涨启动该有首根突破买入"
    assert all(tr["dir"] == "BUY" for tr in trades)
