"""squeeze 模板回归(2026-08-16): 压缩后突破的已知答案 + 无压缩不做/一次性/
长线过滤 的反例 + 真引擎端到端。改模板前后必跑。

构造口径: n_ref=100, n_channel=20, atr_n=14 → warmup=119;
常态 = 100±0.2 锯齿(TR≈0.5), 压缩 = ±0.02 平贴(TR≈0.2) → 比值 0.4。
"""
import numpy as np
import pytest

from src.services.backtest import run_backtest
from strategy_core.templates.squeeze import Squeeze

P = {"n_ref": 100, "n_channel": 20, "atr_n": 14, "sq_ratio": 0.7,
     "buf_atr": 0.25, "k_sl": 2.0, "rr": 2.0, "trend_filter": 0}


def mk(c_arr, **over):
    strat = Squeeze({**P, **over}, 0.01)
    c = np.asarray(c_arr, float)
    return strat, c.copy(), c + 0.1, c - 0.1, c


def ref_atr(h, l, c, n=14, skip_last=False):
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    if skip_last:
        tr = tr[:-1]
    return float(tr[-n:].mean())


def zig(n, amp=0.2, base=100.0):
    return [base + (amp if i % 2 else -amp) for i in range(n)]


def compress_break(last=100.8, n_compress=25):
    """94 根常态锯齿 + n_compress 根压缩平贴 + 末根突破 → 120 根"""
    return np.array(zig(120 - n_compress - 1) + zig(n_compress, amp=0.02) + [last])


def test_squeeze_breakout_buy_exact_sl_tp():
    strat, o, h, l, c = mk(compress_break())
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    atr_now = ref_atr(h, l, c)
    assert sig.sl == pytest.approx(100.8 - 2.0 * atr_now)
    assert sig.tp == pytest.approx(100.8 + 2.0 * 2.0 * atr_now)


def test_no_squeeze_no_trade():
    # 全程常态锯齿(比值≈1 > 0.7): 同样的突破价也不做 — 没蹲守就没蓄能
    strat, o, h, l, c = mk(np.array(zig(119) + [100.8]))
    assert strat.on_bar(o, h, l, c) is None


def test_one_shot_second_breakout_bar():
    # 前一根已破通道(100.7), 当前 101.0 是第二根 → 不追
    arr = compress_break(last=101.0)
    arr[-2] = 100.7
    strat, o, h, l, c = mk(arr)
    assert strat.on_bar(o, h, l, c) is None


def test_trend_filter_blocks_countertrend_break():
    # 长线向下(锯齿中枢 108→100)后压缩, 向上突破: trend_filter=1 拦, =0 放
    desc = [108.0 - 0.08 * i + (0.2 if i % 2 else -0.2) for i in range(94)]
    arr = np.array(desc + zig(25, amp=0.02) + [100.8])
    strat, o, h, l, c = mk(arr, trend_filter=1)
    assert strat.on_bar(o, h, l, c) is None
    strat, o, h, l, c = mk(arr, trend_filter=0)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"


def test_valid_params():
    assert Squeeze.valid_params(P)
    assert not Squeeze.valid_params({**P, "sq_ratio": 1.5})
    assert not Squeeze.valid_params({**P, "n_channel": 200})   # 通道大于常态窗


def _m1_normal_compress_rise():
    t, c = [], []
    t0 = 1700000000 - 1700000000 % 3600
    levels = [100.0 + (0.2 if k % 2 else -0.2) for k in range(200)]   # 常态锯齿
    levels += [100.0 + (0.02 if k % 2 else -0.02) for k in range(30)]  # 压缩
    levels += [100.0 + 1.0 * j for j in range(40)]                     # 扩张单边
    for k, lvl in enumerate(levels):
        for m in range(60):
            t.append(t0 + k * 3600 + m * 60)
            c.append(lvl + (0.02 if m % 2 else -0.02))
    c = np.array(c)
    return {"time": np.array(t, np.int64), "open": c.copy(), "high": c + 0.03,
            "low": c - 0.03, "close": c, "spread": np.full(len(t), 10, np.int64)}


def test_engine_end_to_end_buys_expansion():
    res = run_backtest(_m1_normal_compress_rise(), "squeeze", P, 0.01, "H1", oos_split=None)
    trades = res["trades"]
    assert trades, "压缩后的向上扩张该有突破买入"
    assert all(tr["dir"] == "BUY" for tr in trades)
