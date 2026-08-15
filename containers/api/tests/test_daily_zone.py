"""daily_zone 模板回归(2026-08-14): 三个信号各一条手工构造的已知答案 +
一次性语义 / 双向冲突 / 振幅过滤 的反例。改模板前后必跑(与 test_engine 同一张网)。

构造口径: day_bars=24。参考块(窗口[-48:-24]) 高 110 低 90;
zone_pct=2 → zone=1.1 → 高带[108.9,111.1] 低带[88.9,91.1]; dist_pct=1。
"""
import numpy as np
import pytest

from strategy_core.templates.daily_zone import DailyZone

P = {"signal": 1, "day_bars": 24, "zone_pct": 2.0, "dist_pct": 1.0,
     "tp_pct": 0.5, "sl_pct": 1.5, "atr_on": 0, "atr_period": 4,
     "atr_min_pct": 25.0, "atr_max_pct": 150.0}


def mk(closes_today, ref_high=110.0, ref_low=90.0, highs_today=None, lows_today=None,
       **over):
    """前置块拉满 warmup, 参考块定死高低, 今日 24 根由用例给出 → (策略, o,h,l,c)"""
    p = {**P, **over}
    strat = DailyZone(p, 0.01)
    d = p["day_bars"]
    n_pre = strat.warmup - 2 * d
    pre = np.full(max(n_pre, 0) + d, 100.0)          # 更早的填充 + 参考块占位
    ref = np.full(d, 100.0)
    c = np.concatenate([pre[:-d] if n_pre > 0 else pre[:0], ref, np.asarray(closes_today, float)])
    h = c.copy(); l = c.copy()
    # 参考块的高低点(决定带的位置)
    h[-2 * d:-d] = ref_high; l[-2 * d:-d] = ref_low
    if highs_today is not None:
        h[-d:] = np.maximum(h[-d:], np.asarray(highs_today, float))
    if lows_today is not None:
        l[-d:] = np.minimum(l[-d:], np.asarray(lows_today, float))
    return strat, c.copy(), h, l, c


def today(at=None, fill=100.0):
    """24 根今日收盘, 默认远离两条带; at = {下标: 值}"""
    a = np.full(24, fill)
    for i, v in (at or {}).items():
        a[i] = v
    return a


# ---------- S1 突破回踩 ----------

def test_s1_breakout_retest_buy():
    # 曾收在带外上方(113>111.1)且摸到 thr(112.211), 现回到带内(110) → 买
    t = today({5: 113.0}, fill=110.0)
    strat, o, h, l, c = mk(t, highs_today=today({5: 113.5}, fill=110.0))
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    assert sig.sl == pytest.approx(110.0 * (1 - 0.015))
    assert sig.tp == pytest.approx(110.0 * (1 + 0.005))


def test_s1_no_break_no_signal():
    # 一直在带内没出去过 → 无信号(EA: 找不到离带 bar 就不判)
    strat, o, h, l, c = mk(today(fill=110.0))
    assert strat.on_bar(o, h, l, c) is None


def test_s1_between_zones_none():
    # 现价在两带之间(100) → 无信号
    strat, o, h, l, c = mk(today({5: 113.0}))
    assert strat.on_bar(o, h, l, c) is None


# ---------- S2 穿越全程 ----------

def test_s2_traverse_buy_fires_on_completing_bar():
    # 先碰低带(91 ≤ 91.1), 当前 bar 破高带+1%(112.3 ≥ 112.211) → 买
    t = today({3: 91.0, 23: 112.3})
    strat, o, h, l, c = mk(t, signal=2)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"


def test_s2_one_shot_only_current_bar():
    # 同样的序列但完成在第 20 根(当前是 23) → 一次性语义: 不追, 无信号
    t = today({3: 91.0, 20: 112.3})
    strat, o, h, l, c = mk(t, signal=2)
    assert strat.on_bar(o, h, l, c) is None


# ---------- S3 碰-离-再碰 ----------

def test_s3_touch_pull_retouch_sell():
    # 高带侧: 碰 hBot(109≥108.9) → 跌离 1%(107.8 以下: 107) → 当前回带内(110) → 卖
    t = today({3: 109.0, 10: 107.0, 23: 110.0})
    strat, o, h, l, c = mk(t, signal=3)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "SELL"


def test_s3_sequence_order_matters():
    # 顺序错: 回落(107)发生在碰带(109)之前, 碰带之后再无回落 → 无信号。
    # fill 必须落在 (回落阈107.8, hBot 108.9) 之间 — 初版用 fill=100 低于回落阈,
    # 等于无意中构造出合法的碰(10)→离(11)→再碰(23), SELL 触发反而是对的(模板没错测试错)
    t = today({3: 107.0, 10: 109.0, 23: 110.0}, fill=108.5)
    strat, o, h, l, c = mk(t, signal=3)
    assert strat.on_bar(o, h, l, c) is None


# ---------- 双向冲突 → 保守不开 ----------

def test_conflict_both_sides_none():
    # 双向同触发 → 0 → 保守不开。可构造性推演(2026-08-14):
    #   S1 的冲突数学上不可达 — 证明 wasAbove 的收盘(>hTop>lTop)必然把低带的
    #   "最后离带方向"改写成向上, 反之亦然, 两边永远互斥(EA 里 res=0 那支是死代码);
    #   S2 也不可达(同一根 bar 不可能同时 ≥hTop(1+d) 又 ≤lBot(1-d))。
    #   S3 在带重叠时真能双向同完成 → 用它验 0 分支。
    # zone_pct=40 → 高带[88,132] 低带[68,112]; fill=60 在两带之外(不误触发再碰)。
    # 卖链: 碰(0:110≥88) → 离(2:66≤87.12) → 再碰(23:110∈高带)
    # 买链: 碰(0:110≤112) → 离(4:135≥113.12) → 再碰(23:110∈低带) — 同一根完成 → 冲突
    t = today({0: 110.0, 2: 66.0, 4: 135.0, 23: 110.0}, fill=60.0)
    strat, o, h, l, c = mk(t, signal=3, zone_pct=40.0, dist_pct=1.0)
    assert strat.on_bar(o, h, l, c) is None


# ---------- 振幅过滤 ----------

def test_range_filter_blocks():
    # 信号成立(同 S1 买例), 但前一块振幅是均值的 ~4 倍 > 150% → 被过滤
    t = today({5: 113.0}, fill=110.0)
    strat, o, h, l, c = mk(t, highs_today=today({5: 113.5}, fill=110.0),
                           atr_on=1, atr_period=4)
    # 参考块(k=1)振幅 = 110-90 = 20; 更早三块 h=l=100 振幅≈0 → pct≈400% > 150
    assert strat.on_bar(o, h, l, c) is None


def test_range_filter_passes_when_even():
    # 四块振幅拉平(都 20) → pct=100% ∈ [25,150] → 放行, 信号照发
    t = today({5: 113.0}, fill=110.0)
    strat, o, h, l, c = mk(t, highs_today=today({5: 113.5}, fill=110.0),
                           atr_on=1, atr_period=4)
    d = 24
    for k in range(2, 5):                    # 把更早三块也造出 20 的振幅
        h[-(k + 1) * d:-k * d] = 110.0
        l[-(k + 1) * d:-k * d] = 90.0
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"


# ---------- 参数校验 ----------

def test_valid_params():
    assert DailyZone.valid_params(P)
    assert not DailyZone.valid_params({**P, "day_bars": 10})
    assert not DailyZone.valid_params({**P, "atr_min_pct": 200.0})
