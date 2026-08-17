"""pullback 模板回归(2026-08-16): 低吸买的已知答案 + 恢复确认/一次性/深度/
不追高/空头镜像 的反例 + 真引擎端到端。改模板前后必跑。

构造口径: n_filter=100, n_high=20, n_pull=5, atr_n=14 → warmup=119; h=c+0.1, l=c-0.1。
"""
import numpy as np
import pytest

from src.services.backtest import run_backtest
from strategy_core.templates.pullback import Pullback

P = {"n_filter": 100, "n_high": 20, "n_pull": 5, "retrace_atr": 1.5,
     "atr_n": 14, "sl_buf_atr": 0.5, "rr": 3.0}


def mk(c_arr, **over):
    strat = Pullback({**P, **over}, 0.01)
    c = np.asarray(c_arr, float)
    return strat, c.copy(), c + 0.1, c - 0.1, c


def ref_atr(h, l, c, n=14):
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(tr[-n:].mean())


def up_dip(tail):
    """114 根上升(95→100.65) + tail → 拼满"""
    return np.array([95.0 + 0.05 * i for i in range(114)] + list(tail))


def test_buy_pullback_exact_sl_tp():
    # 回调 100.2→99.0, 末根 99.6 收复前根最高(99.1), 仍低于趋势高点(100.75) → 低吸买
    strat, o, h, l, c = mk(up_dip([100.2, 99.8, 99.4, 99.0, 99.6]))
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    atr = ref_atr(h, l, c)
    dip = float(l[-5:].min())                    # 回调低点 98.9
    assert sig.sl == pytest.approx(dip - 0.5 * atr)
    assert sig.tp == pytest.approx(99.6 + 3.0 * (99.6 - sig.sl))


def test_no_recovery_still_falling():
    strat, o, h, l, c = mk(up_dip([100.2, 99.8, 99.4, 99.0, 98.6]))
    assert strat.on_bar(o, h, l, c) is None      # 还在跌 → 不接飞刀


def test_one_shot_second_recovery_bar():
    # 前一根已收复(99.6 > 99.5), 当前 99.8 是第二根 → 不追
    strat, o, h, l, c = mk(up_dip([99.8, 99.4, 99.0, 99.6, 99.8]))
    assert strat.on_bar(o, h, l, c) is None


def test_shallow_retrace_blocked():
    # 同样的形态但把深度门槛抬到 3×ATR(≈0.96 > 实际回调 0.45) → 被深度闸拦下
    strat, o, h, l, c = mk(up_dip([100.55, 100.5, 100.45, 100.4, 100.55]),
                           retrace_atr=3.0)
    assert strat.on_bar(o, h, l, c) is None


def test_back_at_high_is_breakout_not_pullback():
    # 恢复过猛直接收到趋势高点上方(101 > hh) → 那是突破(fable 地盘), 低吸不做
    strat, o, h, l, c = mk(up_dip([100.2, 99.8, 99.4, 99.0, 101.0]))
    assert strat.on_bar(o, h, l, c) is None


def test_sell_rip_in_downtrend():
    # 镜像: 长线向下, 反弹 99.8→101.0 后首根跌破前根最低 → 卖反弹
    base = [105.0 - 0.05 * i for i in range(114)]          # 105→99.35
    strat, o, h, l, c = mk(np.array(base + [99.8, 100.2, 100.6, 101.0, 100.4]))
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "SELL"


def test_valid_params():
    assert Pullback.valid_params(P)
    assert not Pullback.valid_params({**P, "n_pull": 30})   # 回调窗大于极值窗
    assert not Pullback.valid_params({**P, "retrace_atr": 0})


def _m1_uptrend_dip_recover():
    t, c = [], []
    t0 = 1700000000 - 1700000000 % 3600
    levels = [95.0 + 0.1 * k for k in range(200)]           # 95 → 114.9
    levels += [113.7, 112.5, 111.3]                         # 急跌
    levels += [111.8 + 0.2 * j for j in range(42)]          # 恢复并回升
    for k, lvl in enumerate(levels):
        for m in range(60):
            t.append(t0 + k * 3600 + m * 60)
            c.append(lvl + (0.02 if m % 2 else -0.02))
    c = np.array(c)
    return {"time": np.array(t, np.int64), "open": c.copy(), "high": c + 0.03,
            "low": c - 0.03, "close": c, "spread": np.full(len(t), 10, np.int64)}


def test_engine_end_to_end_buys_recovery():
    res = run_backtest(_m1_uptrend_dip_recover(), "pullback", P, 0.01, "H1", oos_split=None)
    trades = res["trades"]
    assert trades, "回调后的首根收复该有低吸买入"
    assert all(tr["dir"] == "BUY" for tr in trades)
