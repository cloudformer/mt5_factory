"""regime_flip 模板回归(2026-08-22): 格龄赌翻转 — 恰好满龄开注/一段一注/
方向与镜像/align 顺长过滤/ATR 括号距离/参数校验。改模板前后必跑。

构造口径: n_long=30, n_short=5, atr_n=5, min_dwell=4 → warmup=44; h=c+0.1, l=c-0.1。
"""
import numpy as np

from strategy_core.templates import TEMPLATES
from strategy_core.templates.regime_flip import RegimeFlip

P = {"n_long": 30, "n_short": 5, "atr_n": 5, "min_dwell": 4,
     "k_sl": 2.0, "rr": 2.0, "align": 0}


def mk(c_arr, **over):
    strat = RegimeFlip({**P, **over}, 0.01)
    c = np.asarray(c_arr, float)
    return strat, c.copy(), c + 0.1, c - 0.1, c


def ref_atr(h, l, c, n=5):
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(tr[-n:].mean())


def flat_then_up(n_up):
    """40 根横盘(100) + n_up 根每根+1 → 短维 A 连续 n_up 根"""
    return np.array([100.0] * 40 + [100.0 + i for i in range(1, n_up + 1)])


def test_registered():
    assert TEMPLATES["regime_flip"] is RegimeFlip


def test_sell_fires_at_exact_dwell():
    strat, o, h, l, c = mk(flat_then_up(4))
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "SELL"
    atr = ref_atr(h, l, c)
    assert abs(sig.sl - (c[-1] + 2.0 * atr)) < 1e-9          # SL = k_sl×ATR
    assert abs(sig.tp - (c[-1] - 2.0 * (2.0 * atr))) < 1e-9  # TP = rr×SL


def test_no_fire_before_or_after_dwell():
    for n_up in (3, 5, 9):   # 未满龄 / 过龄(一段只赌 dwell==N 那一根)
        strat, o, h, l, c = mk(flat_then_up(n_up))
        assert strat.on_bar(o, h, l, c) is None


def test_buy_mirror_on_down_run():
    # 横盘(收盘==均线 → B) 先补一根上破切换到 A, 再跌 4 根 → 短维 B 恰好满龄
    c_arr = np.array([100.0] * 40 + [101.0, 99.5, 98.5, 97.5, 96.5])
    strat, o, h, l, c = mk(c_arr)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    assert sig.sl < c[-1] < sig.tp


def test_align_blocks_counter_long_bet():
    # 长维↑(收盘在 SMA30 上) + 短维 A 满龄 → 赌翻空 = 逆长势: align=1 拦, align=0 放
    strat0, o, h, l, c = mk(flat_then_up(4), align=0)
    assert strat0.on_bar(o, h, l, c) is not None
    strat1, o, h, l, c = mk(flat_then_up(4), align=1)
    assert strat1.on_bar(o, h, l, c) is None


def test_align_allows_dip_buy_in_long_up():
    # 强升 35 根后回落 4 根(仍在长均线上): 短维 B 满龄 + 长维↑ → 顺长买回调, align=1 放行
    c_arr = np.array([100.0 + i for i in range(36)] + [131.0, 129.5, 128.0, 126.5])
    strat, o, h, l, c = mk(c_arr, align=1)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    assert c[-1] > float(c[-30:].mean())   # 前提自检: 确实长维↑


def test_valid_params():
    ok = dict(P)
    assert RegimeFlip.valid_params(ok)
    assert not RegimeFlip.valid_params({**P, "n_short": 40})   # 短 > 长
    assert not RegimeFlip.valid_params({**P, "min_dwell": 1})
    assert not RegimeFlip.valid_params({**P, "align": 2})
    assert not RegimeFlip.valid_params({**P, "rr": 0})
