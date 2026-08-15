"""weekly_day 模板回归(2026-08-15): 手工构造的已知答案 + 一次性语义/星期判定/
ATR 过滤/历史不足 的反例 + 一次真引擎端到端(warmup·周末·半天窗口的联动)。
改模板前后必跑(与 test_engine 同一张网)。

构造口径: epoch 日 20003 = 周一((20003+4)%7=1, MT5 口径 0=周日..6=周六);
每个"日"造 4 根 bar(6 小时一根), 日 OHLC 由聚合口径读出(首根开/末根收/极值)。
"""
import numpy as np
import pytest

import strategy_core
from src.services.backtest import run_backtest
from strategy_core.templates.weekly_day import WeeklyDay

MON, TUE, WED, THU, FRI = 20010, 20011, 20012, 20013, 20014   # 20010=周一
P = {"check_day": 1, "direction": 1, "atr_on": 0, "atr_min": 1.5,
     "atr_period": 3, "k_atr": 0.5, "rr": 2.0, "day_bars": 96}

# 填充日 o=100 h=101 l=99 c=100; 周一 o=100 h=102 l=99.5 c=101.5(阳线)
FILL = (100.0, 101.0, 99.0, 100.0)
MON_BULL = (100.0, 102.0, 99.5, 101.5)
# TR: 填充日=2, 周一=max(2.5, |102-100|, |99.5-100|)=2.5 → atr(3)=(2+2+2.5)/3
ATR = (2.0 + 2.0 + 2.5) / 3


def mk(day_specs, cur_day, cc=101.0, cur_bars=1, **over):
    """day_specs = [(epoch日, o, h, l, c), ...] 完整日 + 当前日 cur_bars 根 → (策略, o,h,l,c)"""
    strat = WeeklyDay({**P, **over}, 0.01)
    T, O, H, L, C = [], [], [], [], []
    for d, do, dh, dl, dc in day_specs:
        for k in range(4):
            T.append(d * 86400 + k * 21600)
            O.append(do); H.append(dh); L.append(dl)
            C.append(dc if k == 3 else do)
    for k in range(cur_bars):
        T.append(cur_day * 86400 + k * 21600)
        O.append(cc); H.append(cc); L.append(cc); C.append(cc)
    strat.t = np.array(T, np.int64)
    return strat, np.array(O), np.array(H), np.array(L), np.array(C)


# 周三~周五填充 + 周一(阳) → 当前周二首根; uds=4 天 = atr_period+1
WEEK = [(20005, *FILL), (20006, *FILL), (20007, *FILL), (MON, *MON_BULL)]


def test_reverse_bullish_monday_sells_tuesday():
    strat, o, h, l, c = mk(WEEK, TUE)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "SELL"
    assert sig.sl == pytest.approx(101.0 + 0.5 * ATR)
    assert sig.tp == pytest.approx(101.0 - 2.0 * 0.5 * ATR)


def test_direct_bullish_monday_buys():
    strat, o, h, l, c = mk(WEEK, TUE, direction=0)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    assert sig.sl == pytest.approx(101.0 - 0.5 * ATR)


def test_one_shot_only_first_bar_of_day():
    # 当前是周二第 2 根 bar → 错过不追(EA 会重试, 我们无状态不追, 声明差异 3)
    strat, o, h, l, c = mk(WEEK, TUE, cur_bars=2)
    assert strat.on_bar(o, h, l, c) is None


def test_wrong_weekday_none():
    # check_day=2(周二), 但上一交易日是周一 → 无信号
    strat, o, h, l, c = mk(WEEK, TUE, check_day=2)
    assert strat.on_bar(o, h, l, c) is None


def test_weekend_gap_friday_fires_monday():
    # check_day=5(周五): 周五收盘后隔周末, 周一(20017)首根上判定"上一交易日=周五" → 反手卖
    days = [(20011, *FILL), (20012, *FILL), (20013, *FILL), (20014, *MON_BULL)]
    strat, o, h, l, c = mk(days, 20017, check_day=5)   # 20014=周五, 20017=下周一
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "SELL"


def test_atr_filter_blocks():
    # 周一振幅 2.5 < 1.5×ATR(≈3.25) → 被过滤
    strat, o, h, l, c = mk(WEEK, TUE, atr_on=1)
    assert strat.on_bar(o, h, l, c) is None


def test_atr_filter_passes():
    # 阈值降到 1.0×ATR(≈2.17) ≤ 2.5 → 放行
    strat, o, h, l, c = mk(WEEK, TUE, atr_on=1, atr_min=1.0)
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "SELL"


def test_insufficient_history_none():
    # 只有 2 个完整日 < atr_period+1 → 不做(EA OnInit 同款)
    strat, o, h, l, c = mk([(20007, *FILL), (MON, *MON_BULL)], TUE)
    assert strat.on_bar(o, h, l, c) is None


def test_no_timestamps_none():
    strat = WeeklyDay(P, 0.01)          # 不挂 t(老引擎/异常) → 不交易不猜
    px = np.full(8, 100.0)
    assert strat.on_bar(px, px, px, px) is None


def test_valid_params():
    assert WeeklyDay.valid_params(P)
    assert not WeeklyDay.valid_params({**P, "check_day": 7})
    assert not WeeklyDay.valid_params({**P, "k_atr": 0})      # 无 SL 裸测不支持(铁律)
    assert not WeeklyDay.valid_params({**P, "day_bars": 10})


# ---------- 真引擎端到端: warmup / 周末缺口 / 半天窗口的联动 ----------

def _m1_weekday_weeks(n_days=30):
    """连续工作日的 M1(周末无 bar): 周一单边涨 1.0, 其余日 100±0.3 震荡"""
    t, o, h, l, c = [], [], [], [], []
    d = 20003                                  # 周一
    made = 0
    while made < n_days:
        if (d + 4) % 7 in (0, 6):
            d += 1
            continue
        mon = (d + 4) % 7 == 1
        for m in range(0, 1440, 2):            # 隔分钟造 bar(缺分钟属正常)
            px = 100.0 + (m / 1440.0 if mon else 0.3 * np.sin(m / 60.0))
            t.append(d * 86400 + m * 60)
            o.append(px); c.append(px + 0.001)
            h.append(px + 0.05); l.append(px - 0.05)
        d += 1
        made += 1
    n = len(t)
    return {"time": np.array(t, np.int64), "open": np.array(o), "high": np.array(h),
            "low": np.array(l), "close": np.array(c),
            "spread": np.full(n, 10, np.int64)}


def test_engine_end_to_end_trades_only_after_check_day():
    params = {**P, "atr_period": 3, "day_bars": 24}    # warmup=(3+3)*24+5=149 根 H1
    res = run_backtest(_m1_weekday_weeks(), "weekly_day", params, 0.01, "H1", oos_split=None)
    trades = res["trades"]
    assert trades, "该有交易(每个周一都是阳线, 反手在周二卖)"
    for tr in trades:
        assert (tr["entry_time"] // 86400 + 4) % 7 == 2, "入场必须全在周二"
        assert tr["dir"] == "SELL"
