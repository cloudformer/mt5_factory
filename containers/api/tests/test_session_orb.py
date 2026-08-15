"""session_orb 模板回归(2026-08-15): 每条 = 原 EA 的一条语义 + 手工小行情 + 已知答案。
首个用 self.t 的时段类模板 — 时段定位/一次性配额/窗口边界都在这张网里钉住。

构造口径: M15(tf=900), session 08:00, range_bars=2(区间=08:00~08:30),
window_bars=8(窗口=08:30~10:30), buffer=20点, sl_buffer=30点, rr=2, point=0.0001。
区间: 高 1.1010 低 1.0990 → 触发线 上 1.1030 / 下 1.0970。
"""
import numpy as np
import pytest

from strategy_core.templates.session_orb import SessionOrb

P = {"start_hour": 8, "range_bars": 2, "window_bars": 8,
     "buffer_points": 20, "sl_buffer_points": 30, "rr": 2.0}
DAY = 20000 * 86400          # 任意一天的 0 点(epoch 对齐日界)
TF = 900


def mk(last_hhmm, closes=None, days_offset=0, extra_days=()):
    """从 06:00 到 last_hhmm 的 M15 序列; closes={('HH:MM'): 收盘价} 覆盖个别 bar。
    区间两根(08:00/08:15)的高低点固定为 1.1010/1.0990。extra_days 往前追加整天(测跨日)。"""
    strat = SessionOrb(dict(P), 0.0001)
    times, cl = [], []
    for d in list(extra_days) + [days_offset]:
        base = DAY + d * 86400
        end_min = (int(last_hhmm[:2]) * 60 + int(last_hhmm[3:])) if d == days_offset else 10 * 60
        for m in range(6 * 60, end_min + 1, 15):
            hhmm = f"{m // 60:02d}:{m % 60:02d}"
            times.append(base + m * 60)
            cl.append((closes or {}).get((d, hhmm), 1.1000))
    c = np.array(cl)
    h, l = c + 0.0002, c - 0.0002
    for i, ts in enumerate(times):
        for d in list(extra_days) + [days_offset]:
            base = DAY + d * 86400
            if ts == base + 8 * 3600:            h[i], l[i] = 1.1010, 1.0996
            if ts == base + 8 * 3600 + 900:      h[i], l[i] = 1.1005, 1.0990
    strat.t = np.array(times, np.int64)
    return strat, c + 0, h, l, c


def test_first_breakout_buy():
    # 08:45 收 1.1035 > 1.1030 → 买; SL=低点-30点, TP=entry+2×风险
    strat, o, h, l, c = mk("08:45", {(0, "08:45"): 1.1035})
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"
    assert sig.sl == pytest.approx(1.0960)
    assert sig.tp == pytest.approx(1.1035 + 2 * (1.1035 - 1.0960))


def test_buffer_not_cleared():
    # 1.1025 过了高点但没过 高点+20点 → 不算突破
    strat, o, h, l, c = mk("08:45", {(0, "08:45"): 1.1025})
    assert strat.on_bar(o, h, l, c) is None


def test_one_shot_first_breakout_only():
    # 08:30 已收 1.1040(第一突破), 08:45 再突破 → 配额已用, 不追
    strat, o, h, l, c = mk("08:45", {(0, "08:30"): 1.1040, (0, "08:45"): 1.1035})
    assert strat.on_bar(o, h, l, c) is None


def test_outside_window():
    # 10:30 开盘的 bar 收盘在 10:45 > 窗口截止 10:30 → 不做
    strat, o, h, l, c = mk("10:30", {(0, "10:30"): 1.1035})
    assert strat.on_bar(o, h, l, c) is None


def test_last_window_bar_still_trades():
    # 10:15 开盘、10:30 收盘 = 窗口最后一根 → 照做(与 EA 的 now<=windowEnd 同口径)
    strat, o, h, l, c = mk("10:15", {(0, "10:15"): 1.1035})
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"


def test_inside_range_no_trade():
    # 当前 bar 还在区间内(08:15) → 等区间收完
    strat, o, h, l, c = mk("08:15", {(0, "08:15"): 1.1035})
    assert strat.on_bar(o, h, l, c) is None


def test_sell_mirror():
    strat, o, h, l, c = mk("09:00", {(0, "09:00"): 1.0965})
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "SELL"
    assert sig.sl == pytest.approx(1.1040)      # 高点 1.1010 + 30点
    assert sig.tp == pytest.approx(1.0965 - 2 * (1.1040 - 1.0965))


def test_new_day_resets_quota():
    # 昨天窗口内有过突破 bar(10:00 收 1.1040), 今天首破照样开 — 配额按日重置
    strat, o, h, l, c = mk("08:45", {(-1, "10:00"): 1.1040, (0, "08:45"): 1.1035},
                           extra_days=[-1])
    sig = strat.on_bar(o, h, l, c)
    assert sig is not None and sig.direction == "BUY"


def test_no_time_no_trade():
    # 没有 self.t(老引擎/异常) → 不交易不猜
    strat, o, h, l, c = mk("08:45", {(0, "08:45"): 1.1035})
    strat.t = None
    assert strat.on_bar(o, h, l, c) is None


def test_valid_params():
    assert SessionOrb.valid_params(P)
    assert not SessionOrb.valid_params({**P, "start_hour": 24})
    assert not SessionOrb.valid_params({**P, "rr": 0})
