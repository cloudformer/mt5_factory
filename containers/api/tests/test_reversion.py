"""reversion 模板回归(2026-08-16): 两个信号各一条已知答案 + 确认/一次性/深度/
长线过滤 的反例 + 真引擎端到端。改模板前后必跑(与 test_engine 同一张网)。

构造口径: n_filter=100, n_dip=5, atr_n=14 → warmup=124; h=c+0.1, l=c-0.1。
"""
import numpy as np
import pytest

from src.services.backtest import run_backtest
from strategy_core.templates.reversion import Reversion

P = {"signal": 1, "n_filter": 100, "n_dip": 5, "d_atr": 3.0, "atr_n": 14,
     "sl_buf_atr": 0.5, "rr": 2.0}


def mk(c_arr, **over):
    strat = Reversion({**P, **over}, 0.01)
    c = np.asarray(c_arr, float)
    return strat, c.copy(), c + 0.1, c - 0.1, c


def ref_atr(h, l, c, n=14):
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(tr[-n:].mean())


def dip_up(last=99.15, dip=(100.4, 99.9, 99.4, 98.9)):
    """119 根上升(95→100.9) + 4 根急跌 + 末根给定 → 124 根"""
    return np.array([95.0 + 0.05 * i for i in range(119)] + list(dip) + [last])


def test_s1_buy_dip_in_uptrend_exact_sl_tp():
    strat, o, h, l, c = mk(dip_up())
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    atr = ref_atr(h, l, c)
    lo = float(l[-5:].min())                       # 坑底 98.8
    assert sig.sl == pytest.approx(lo - 0.5 * atr)
    assert sig.tp == pytest.approx(99.15 + 2.0 * (99.15 - sig.sl))


def test_s1_no_confirm_still_falling():
    strat, o, h, l, c = mk(dip_up(last=98.6))      # 末根还在跌 → 不接飞刀
    assert strat.on_bar(o, h, l, c) is None


def test_s1_one_shot_second_bounce_bar():
    # 前一根已经收涨(98.9→99.1), 当前 99.3 是第二根确认 → 不追
    strat, o, h, l, c = mk(dip_up(last=99.3, dip=(100.4, 99.9, 98.9, 99.1)))
    assert strat.on_bar(o, h, l, c) is None


def test_s1_shallow_dip_blocked():
    strat, o, h, l, c = mk(dip_up(last=100.75, dip=(100.85, 100.8, 100.75, 100.7)))
    assert strat.on_bar(o, h, l, c) is None        # 回撤深度不够 → 无浪可回


def test_s1_downtrend_blocks_buy():
    # 长线向下(110→104)出现同样的急跌+确认 → signal1 不接(那是 signal2 的地盘)
    c = np.array([110.0 - 0.05 * i for i in range(119)] + [103.5, 103.0, 102.5, 102.0, 102.25])
    strat, o, h, l, c = mk(c)
    assert strat.on_bar(o, h, l, c) is None


def test_s2_capitulation_buy_needs_engulf():
    # 长线向下 + 深跌 + 吞没级反转(收过前根最高 100.1) → 抄底买
    base = [110.0 - 0.05 * i for i in range(119)]
    strat, o, h, l, c = mk(np.array(base + [103.0, 102.0, 101.0, 100.0, 100.9]), signal=2)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    # 同样构造但确认不过前根最高(100.05 ≤ 100.1) → 不算大反转, 不接
    strat, o, h, l, c = mk(np.array(base + [103.0, 102.0, 101.0, 100.0, 100.05]), signal=2)
    assert strat.on_bar(o, h, l, c) is None


def test_valid_params():
    assert Reversion.valid_params(P)
    assert not Reversion.valid_params({**P, "signal": 3})
    assert not Reversion.valid_params({**P, "n_dip": 1})
    assert not Reversion.valid_params({**P, "n_filter": 3})   # 短于 n_dip


# ---------- 真引擎端到端: 上升趋势中一次急跌 → 首根确认买入 ----------

def _m1_uptrend_with_dip():
    t, c = [], []
    t0 = 1700000000 - 1700000000 % 3600
    levels = [95.0 + 0.1 * k for k in range(200)]            # 95 → 114.9
    levels += [113.7, 112.5, 111.3]                          # 3 根急跌
    levels += [111.8 + 0.2 * j for j in range(42)]           # 确认 + 回升到 TP
    for k, lvl in enumerate(levels):
        for m in range(60):
            t.append(t0 + k * 3600 + m * 60)
            c.append(lvl + (0.02 if m % 2 else -0.02))
    c = np.array(c)
    return {"time": np.array(t, np.int64), "open": c.copy(), "high": c + 0.03,
            "low": c - 0.03, "close": c, "spread": np.full(len(t), 10, np.int64)}


def test_engine_end_to_end_buys_the_dip():
    res = run_backtest(_m1_uptrend_with_dip(), "reversion", P, 0.01, "H1", oos_split=None)
    trades = res["trades"]
    assert trades, "急跌后的首根确认该有买入"
    assert all(tr["dir"] == "BUY" for tr in trades)
