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
