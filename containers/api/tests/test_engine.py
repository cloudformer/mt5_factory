"""引擎回归测试(v2.4): 撮合原语 / 指标 / 对账配对 / run_backtest 集成。
每个用例 = 一条 CLAUDE.md 铁律 + 手工构造的小行情 + 已知正确答案。
改撮合/OOS/对账代码后跑一遍: 全绿=没碰坏既有规则; 红=精确指出坏在哪条。"""
import numpy as np
import pytest

from src.services import backtest as bt
from src.services.backtest import (_metrics, _spread_at, _walk_exit, aggregate,
                                   run_backtest)
from src.routes.backtests import _merge_windows, _reconcile_metrics
from strategy_core.base import Signal


# ---------- 撮合原语: _spread_at ----------

def test_spread_fixed_vs_recorded():
    m1 = {"spread": np.array([20, 30], np.int64)}
    assert _spread_at(m1, 0, 0.01, 5.0) == pytest.approx(0.05)   # 固定点差优先
    assert _spread_at(m1, 1, 0.01, None) == pytest.approx(0.30)  # 无固定 → 用当根记录


# ---------- 撮合原语: _walk_exit (铁律核心) ----------

def _bar(o, h, l, sp=0):
    return {"open": np.array([o]), "high": np.array([h]),
            "low": np.array([l]), "spread": np.array([sp], np.int64)}


def test_buy_sl_hit():
    pos = {"dir": "BUY", "sl": 99.0, "tp": 101.0}
    assert _walk_exit(pos, 0, 1, _bar(100, 100.5, 98.5), 0.01, 0) == (99.0, 0, "sl")


def test_buy_tp_hit():
    pos = {"dir": "BUY", "sl": 99.0, "tp": 101.0}
    assert _walk_exit(pos, 0, 1, _bar(100, 101.5, 99.5), 0.01, 0) == (101.0, 0, "tp")


def test_buy_same_bar_sl_wins():  # 同一根同时碰 SL+TP → 悲观取 SL
    pos = {"dir": "BUY", "sl": 99.0, "tp": 101.0}
    exit_price, _, reason = _walk_exit(pos, 0, 1, _bar(100, 102.0, 98.0), 0.01, 0)
    assert reason == "sl" and exit_price == 99.0


def test_buy_gap_down_open():  # 跳空穿止损: 按实际开盘价成交, 不按挂单价
    pos = {"dir": "BUY", "sl": 99.0, "tp": 101.0}
    assert _walk_exit(pos, 0, 1, _bar(98.0, 98.5, 97.5), 0.01, 0) == (98.0, 0, "sl_gap")


def test_buy_gap_up_open():
    pos = {"dir": "BUY", "sl": 99.0, "tp": 101.0}
    assert _walk_exit(pos, 0, 1, _bar(102.0, 102.5, 101.5), 0.01, 0) == (102.0, 0, "tp_gap")


def test_sell_sl_hit_uses_ask():  # SELL 以 ask(=bid+点差) 离场
    pos = {"dir": "SELL", "sl": 101.0, "tp": 99.0}
    # bid high=100.9, 点差 20*0.01=0.2 → ask high=101.1 >= sl 101 → 命中 sl
    assert _walk_exit(pos, 0, 1, _bar(100, 100.9, 100, sp=20), 0.01, None) == (101.0, 0, "sl")


def test_sell_same_bar_sl_wins():
    pos = {"dir": "SELL", "sl": 101.0, "tp": 99.0}
    # ask 触 sl(high+sp>=101) 且 ask 触 tp(low+sp<=99) → 悲观取 sl
    _, _, reason = _walk_exit(pos, 0, 1, _bar(100, 101.0, 98.9, sp=10), 0.01, None)
    assert reason == "sl"


def test_no_hit_returns_none():
    pos = {"dir": "BUY", "sl": 99.0, "tp": 101.0}
    assert _walk_exit(pos, 0, 1, _bar(100, 100.2, 99.8), 0.01, 0) is None


# ---------- 聚合: M1 → 高周期 ----------

def test_aggregate_ohlc_and_slices():
    # 3根M1同属一个M15桶(time//900相同), 第4根跨桶
    t0 = 900_000
    m1 = {"time": np.array([t0, t0 + 60, t0 + 120, t0 + 900], np.int64),
          "open": np.array([10., 11., 12., 20.]),
          "high": np.array([15., 13., 14., 21.]),
          "low": np.array([9., 8., 11., 19.]),
          "close": np.array([11., 12., 13., 20.5])}
    tf = aggregate(m1, 900)
    assert list(tf["time"]) == [t0, t0 + 900]
    assert tf["open"][0] == 10. and tf["close"][0] == 13.        # 开=首, 收=末
    assert tf["high"][0] == 15. and tf["low"][0] == 8.           # 高=max, 低=min
    assert (int(tf["m1_start"][0]), int(tf["m1_end"][0])) == (0, 3)  # 桶0 = M1[0:3]
    assert (int(tf["m1_start"][1]), int(tf["m1_end"][1])) == (3, 4)


def test_aggregate_missing_minute_safe():  # 缺分钟按时间桶分, 不按根数
    t0 = 900_000
    m1 = {"time": np.array([t0, t0 + 300, t0 + 840], np.int64),  # 中间缺很多分钟, 仍同桶
          "open": np.array([10., 11., 12.]), "high": np.array([15., 13., 14.]),
          "low": np.array([9., 8., 11.]), "close": np.array([11., 12., 13.])}
    tf = aggregate(m1, 900)
    assert len(tf["time"]) == 1 and tf["high"][0] == 15. and tf["low"][0] == 8.


# ---------- 指标 ----------

def test_metrics_pf_winrate_dd():
    trades = [{"points": 100., "entry_time": 1_700_000_000},
              {"points": -40., "entry_time": 1_700_000_000},
              {"points": 60., "entry_time": 1_700_000_000}]
    m = _metrics(trades)
    assert m["trades"] == 3 and m["wins"] == 2
    assert m["win_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert m["net_points"] == 120.0
    assert m["profit_factor"] == pytest.approx(160 / 40, abs=1e-3)  # 毛利160/毛损40
    assert m["max_dd_points"] == 40.0  # 峰100→60 回撤40


def test_metrics_no_loss_pf_none():  # 无亏损: PF=None(视为无穷, 上层特殊处理)
    m = _metrics([{"points": 10., "entry_time": 1_700_000_000}])
    assert m["profit_factor"] is None


def test_metrics_by_year():
    import datetime as _dt
    y2025 = int(_dt.datetime(2025, 6, 1, tzinfo=_dt.timezone.utc).timestamp())
    y2026 = int(_dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc).timestamp())
    m = _metrics([{"points": 10., "entry_time": y2025},
                  {"points": 5., "entry_time": y2026}])
    assert m["by_year"] == {"2025": 10.0, "2026": 5.0}


# ---------- 对账配对 ----------

def _a(ts, d, profit):
    return {"dir": d, "ts": ts, "profit": profit, "entry": "x"}


def _b(ts, d, points):
    return {"dir": d, "entry_time": ts, "points": points, "entry": 1.0, "exit": 1.0}


def test_reconcile_perfect_match():
    t = 1_700_000_000
    m, pairs = _reconcile_metrics(
        [_a(t, "buy", 5), _a(t + 3600, "sell", -3)],
        [_b(t + 10, "BUY", 5), _b(t + 3610, "SELL", -3)], tol=1200)
    assert m["paired"] == 2 and m["union"] == 2
    assert m["count_match_rate"] == 1.0 and m["dir_match_rate"] == 1.0
    assert m["outcome_match_rate"] == 1.0 and m["match_score"] == 100.0
    assert m["q10_pass"] is True


def test_reconcile_missing_bt_lowers_rate():
    t = 1_700_000_000
    m, _ = _reconcile_metrics(
        [_a(t, "buy", 5), _a(t + 3600, "sell", -3)],
        [_b(t + 10, "BUY", 5)], tol=1200)   # 只有一笔回测, 第二笔实盘配不上
    assert m["paired"] == 1 and m["union"] == 2
    assert m["count_match_rate"] == 0.5 and m["q10_pass"] is False


def test_reconcile_direction_mismatch():
    t = 1_700_000_000
    m, _ = _reconcile_metrics([_a(t, "buy", 5)], [_b(t + 10, "SELL", 5)], tol=1200)
    assert m["paired"] == 1 and m["dir_match_rate"] == 0.0   # 配上了但方向反


def test_reconcile_out_of_tol_not_paired():
    t = 1_700_000_000
    m, _ = _reconcile_metrics([_a(t, "buy", 5)], [_b(t + 5000, "BUY", 5)], tol=1200)
    assert m["paired"] == 0   # 超容差不配对


def test_reconcile_one_sided_denominator():
    t = 1_700_000_000
    m, _ = _reconcile_metrics([_a(t, "buy", 5), _a(t + 3600, "sell", -3)],
                              [_b(t + 10, "BUY", 5)], tol=1200, one_sided=True)
    assert m["denominator"] == 2 and m["mode"] == "one_sided"   # 单边分母=实盘笔数


def test_reconcile_unmatched_bt_appended():  # 回测有信号实盘没单 → 抓漏单(附行, 不计率)
    t = 1_700_000_000
    _, pairs = _reconcile_metrics([_a(t, "buy", 5)],
                                  [_b(t + 10, "BUY", 5), _b(t + 99999, "SELL", 3)], tol=1200)
    orphan = [p for p in pairs if p["actual"] is None and p["bt"] is not None]
    assert len(orphan) == 1


# ---------- 窗口合并 ----------

def test_merge_windows_overlap():
    assert _merge_windows([[0, 10], [5, 15], [20, 25]]) == [[0, 15], [20, 25]]


# ---------- run_backtest 集成(桩策略, 全链确定) ----------

class _StubOnce:
    """首次调用发 BUY, 之后 None — 让集成回测恰好开一笔可预测的仓"""
    def __init__(self, *a, **k):
        self._fired = False
    @property
    def warmup(self):
        return 2
    def on_bar(self, o, h, l, c):
        if self._fired:
            return None
        self._fired = True
        return Signal("BUY", sl=99.0, tp=100.5)


def _stub_m1():
    t0 = 1_700_000_000
    n = 5
    return {"time": np.array([t0 + i * 60 for i in range(n)], np.int64),
            "open": np.array([100., 100., 100., 100., 100.]),
            "high": np.array([100., 100., 100., 101., 100.]),   # 仅 bar3 高到能触 TP
            "low": np.array([100., 100., 100., 99.5, 100.]),
            "close": np.array([100., 100., 100., 100.8, 100.]),
            "spread": np.zeros(n, np.int64)}, t0


def test_run_backtest_one_trade(monkeypatch):
    monkeypatch.setattr(bt, "make_strategy", lambda *a, **k: _StubOnce())
    m1, t0 = _stub_m1()
    res = run_backtest(m1, "any", {}, 0.01, "M1",
                       slippage_points=0, commission_points=0, spread_points=0, oos_split=None)
    assert len(res["trades"]) == 1
    tr = res["trades"][0]
    # 收盘bar决策→下一根开盘进场; 进场bar=M1[3]; TP=100.5, 进场=100.0 → (100.5-100)/0.01=50点
    assert tr["dir"] == "BUY" and tr["reason"] == "tp"
    assert tr["entry"] == 100.0 and tr["exit"] == 100.5
    assert tr["points"] == 50.0
    assert tr["entry_time"] == t0 + 180   # 进场在 bar3(不偷看未来: 在 bar2 收盘后)


def test_run_backtest_start_ts_blocks_entry(monkeypatch):
    monkeypatch.setattr(bt, "make_strategy", lambda *a, **k: _StubOnce())
    m1, t0 = _stub_m1()
    res = run_backtest(m1, "any", {}, 0.01, "M1", start_ts=t0 + 10_000,
                       slippage_points=0, commission_points=0, spread_points=0, oos_split=None)
    assert res["trades"] == []   # 起点在所有bar之后 → 零开仓(对账重放起点对齐用)


def test_run_backtest_commission_deducted(monkeypatch):
    monkeypatch.setattr(bt, "make_strategy", lambda *a, **k: _StubOnce())
    m1, _ = _stub_m1()
    res = run_backtest(m1, "any", {}, 0.01, "M1",
                       slippage_points=0, commission_points=7, spread_points=0, oos_split=None)
    assert res["trades"][0]["points"] == pytest.approx(50.0 - 7)   # 佣金按点从盈亏扣


# ---------- 移动止损(v0.9): 纯函数 trail_new_sl ----------

from strategy_core.trailing import atr_m1, trail_new_sl  # noqa: E402

_FIXED = {"active": "fixed", "fixed": {"gap": 50}}                       # gap 50点=0.5(point 0.01)
_BE = {"active": "breakeven", "breakeven": {"gap": 50, "start": 100}}    # 盈利100点(1.0)才启动
_ATR = {"active": "atr", "atr": {"k": 2.0, "period": 14}}


def test_trail_ratchet_up_only_buy():
    # BUY entry=100, cur_sl=99: ref=101 → cand=100.5 > 99 → 移
    assert trail_new_sl("BUY", 100, 99.0, 101.0, _FIXED, 0.01) == pytest.approx(100.5)
    # ref 回落到 100.6 → cand=100.1 < 已在 100.5 的 SL → None(只上不下)
    assert trail_new_sl("BUY", 100, 100.5, 100.6, _FIXED, 0.01) is None


def test_trail_sell_mirror():
    # SELL entry=100, cur_sl=101: ref(ask)=99 → cand=99.5 < 101 → 移
    assert trail_new_sl("SELL", 100, 101.0, 99.0, _FIXED, 0.01) == pytest.approx(99.5)
    # 反弹 ref=99.8 → cand=100.3 > 已在 99.5 → None
    assert trail_new_sl("SELL", 100, 99.5, 99.8, _FIXED, 0.01) is None


def test_trail_breakeven_gate_and_jump():
    # 盈利 0.9 < start 1.0 → 不启动
    assert trail_new_sl("BUY", 100, 99.0, 100.9, _BE, 0.01) is None
    # 盈利 1.0 达标 → cand=max(ref-gap, entry)=max(100.5, 100)=100.5
    assert trail_new_sl("BUY", 100, 99.0, 101.0, _BE, 0.01) == pytest.approx(100.5)
    # 刚过启动但 ref-gap<entry → 至少保本(=entry)
    be = {"active": "breakeven", "breakeven": {"gap": 200, "start": 100}}
    assert trail_new_sl("BUY", 100, 99.0, 101.0, be, 0.01) == pytest.approx(100.0)


def test_trail_breakeven_requires_start():
    assert trail_new_sl("BUY", 100, 99.0, 105.0,
                        {"active": "breakeven", "breakeven": {"gap": 50}}, 0.01) is None


def test_trail_atr_gap():
    # atr=0.3, k=2 → gap=0.6: ref=102 → cand=101.4
    assert trail_new_sl("BUY", 100, 99.0, 102.0, _ATR, 0.01, atr=0.3) == pytest.approx(101.4)
    assert trail_new_sl("BUY", 100, 99.0, 102.0, _ATR, 0.01, atr=None) is None  # 无ATR不动


def test_trail_disabled_or_bad_cfg():
    assert trail_new_sl("BUY", 100, 99.0, 105.0, {}, 0.01) is None
    assert trail_new_sl("BUY", 100, 99.0, 105.0, {"active": None}, 0.01) is None
    assert trail_new_sl("BUY", 100, 99.0, 105.0, {"active": "fixed", "fixed": {}}, 0.01) is None


def test_combo_error_strict_and_trail_error_validates():
    """严格分离: 生成策略管道拒收 trail 键(插件调优走第4步); trail_error 单独验插件结构"""
    from src.services.instances import combo_error, trail_error
    from strategy_core import TEMPLATES
    cls = TEMPLATES["ma_cross"]
    space = cls.RANDOM_SPACE or cls.PARAM_GRID
    base = {k: (v[0] if isinstance(v, tuple) else v[0]) for k, v in space.items()}
    assert combo_error(cls, space, base) is None                          # 正常参数照旧
    withtrail = {**base, "trail": {"active": "atr", "atr": {"k": 4.0}}}
    assert combo_error(cls, space, withtrail) is not None                 # 带 trail 拒收(分离)
    assert trail_error({"active": "atr", "atr": {"k": 4.0, "period": 14}}) is None
    assert "start" in trail_error({"active": "breakeven", "breakeven": {"gap": 50}})
    assert trail_error({"active": "fixed", "fixed": {}}) is not None      # gap 缺失拒收
    assert trail_error({"active": "nope"}) is not None


def test_atr_m1_window_and_insufficient():
    m1 = {"high": np.array([2., 3., 2., 3.]), "low": np.array([1., 1., 1., 1.]),
          "close": np.array([1.5, 2., 1.5, 2.])}
    assert atr_m1(m1, 1, 3) is None                       # 历史不足
    # j=3, period=3: 窗 h[1..3] l[1..3] cp=close[0..2] → tr=[max(2,1.5,.5)=2, max(1,0,1)=1, max(2,1.5,.5)=2]
    assert atr_m1(m1, 3, 3) == pytest.approx((2 + 1 + 2) / 3)


# ---------- 移动止损: 引擎集成(_walk_exit / run_backtest) ----------

def _m1_seq(prices):
    """按 (o,h,l,c) 序列造 M1(零点差)"""
    n = len(prices)
    return {"time": np.array([1_700_000_000 + i * 60 for i in range(n)], np.int64),
            "open": np.array([p[0] for p in prices]),
            "high": np.array([p[1] for p in prices]),
            "low": np.array([p[2] for p in prices]),
            "close": np.array([p[3] for p in prices]),
            "spread": np.zeros(n, np.int64)}


def test_walk_exit_trailing_locks_profit_buy():
    # entry=100, sl=99, tp=110(远); 走势: 涨到101.5收盘 → SL棘轮到101; 次根跌到100.5 → 触发被追后的SL
    m1 = _m1_seq([(100.5, 101.6, 100.4, 101.5),   # bar0: 不触发, 收盘后 SL→101.5-0.5=101
                  (101.1, 101.2, 100.5, 100.6)])  # bar1: low 100.5 <= 101 → sl 触发(在被追的位置)
    pos = {"dir": "BUY", "entry": 100.0, "sl": 99.0, "tp": 110.0}
    hit = _walk_exit(pos, 0, 2, m1, 0.01, 0, trail=_FIXED)
    assert hit == (101.0, 1, "sl") and pos["trailed"] is True   # 出在101(锁利), 非原始99


def test_walk_exit_no_trail_identical_old_path():
    # 同一行情不开 trail: SL 停 99, bar1 不触发 → None(与旧行为一致)
    m1 = _m1_seq([(100.5, 101.6, 100.4, 101.5), (101.0, 101.2, 100.5, 100.6)])
    pos = {"dir": "BUY", "entry": 100.0, "sl": 99.0, "tp": 110.0}
    assert _walk_exit(pos, 0, 2, m1, 0.01, 0) is None and pos["sl"] == 99.0


def test_walk_exit_trailing_sell_uses_ask():
    # SELL entry=100, sl=101: 跌到98.5收盘, 点差20点(0.2) → ask=98.7 → SL→98.7+0.5=99.2
    m1 = _m1_seq([(99.5, 99.6, 98.4, 98.5)])
    m1["spread"] = np.array([20], np.int64)
    pos = {"dir": "SELL", "entry": 100.0, "sl": 101.0, "tp": 90.0}
    assert _walk_exit(pos, 0, 1, m1, 0.01, None, trail=_FIXED) is None
    assert pos["sl"] == pytest.approx(99.2)


class _StubTrail(_StubOnce):
    """TP 放远(110): 专测 trailing — 不被 _StubOnce 的近 TP(100.5) 抢先"""
    def on_bar(self, o, h, l, c):
        if self._fired:
            return None
        self._fired = True
        return Signal("BUY", sl=99.0, tp=110.0)


def test_run_backtest_trail_reason_tsl(monkeypatch):
    """集成: 开固定移动 → 冲高回落笔的出场 reason=tsl 且净点优于不开"""
    monkeypatch.setattr(bt, "make_strategy", lambda *a, **k: _StubTrail())
    t0 = 1_700_000_000
    n = 6
    m1 = {"time": np.array([t0 + i * 60 for i in range(n)], np.int64),
          "open": np.array([100., 100., 100., 100., 101.5, 101.]),
          "high": np.array([100., 100., 100., 101.6, 101.6, 101.]),
          "low": np.array([100., 100., 100., 100., 100.9, 99.]),
          "close": np.array([100., 100., 100., 101.5, 101., 99.5]),
          "spread": np.zeros(n, np.int64)}
    params_on = {"trail": {"active": "fixed", "fixed": {"gap": 30}}}
    res_on = run_backtest(m1, "any", params_on, 0.01, "M1",
                          slippage_points=0, commission_points=0, spread_points=0, oos_split=None)
    monkeypatch.setattr(bt, "make_strategy", lambda *a, **k: _StubTrail())
    res_off = run_backtest(m1, "any", {}, 0.01, "M1",
                           slippage_points=0, commission_points=0, spread_points=0, oos_split=None)
    # 开: bar3收盘101.5 → SL棘轮到101.2; bar4 low 100.9 触发 → tsl, +120点
    tr_on, tr_off = res_on["trades"][0], res_off["trades"][0]
    assert tr_on["reason"] == "tsl" and tr_on["points"] == pytest.approx(120.0)
    # 关: SL 停 99, bar5 low 99 触发原始止损 → -100点; 开优于关(锁利)
    assert tr_off["reason"] == "sl" and tr_off["points"] == pytest.approx(-100.0)


def test_run_backtest_keep_tp_false_removes_tp(monkeypatch):
    monkeypatch.setattr(bt, "make_strategy", lambda *a, **k: _StubOnce())
    m1, _ = _stub_m1()   # bar3 high=101 会触发原TP 100.5
    params = {"trail": {"active": "fixed", "fixed": {"gap": 500}, "keep_tp": False}}
    res = run_backtest(m1, "any", params, 0.01, "M1",
                       slippage_points=0, commission_points=0, spread_points=0, oos_split=None)
    assert res["trades"] == []   # TP被去掉且SL(gap极大)未被触发 → 持仓到数据尾不成交
