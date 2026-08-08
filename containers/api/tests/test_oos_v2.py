"""OOS 筛选 v2(v0.6) 判定回归网: 切点时间学 / PF 三边界 / 两层门槛 / 配置校验。
每个用例 = 设计文档里的一条定版口径 + 手工构造的已知答案。
改 services/oos_v2.py 前后必跑 — 红 = 改了口径, 须重新和 Frank 定版。"""
from datetime import date, timedelta, timezone

import pytest

from src.services.oos_v2 import (YEAR_DAYS, anchor_dt, cfg_params, judge_one,
                                 judge_trades, seg_window, summarize,
                                 window_years)

ANCHOR = date(2026, 8, 6)

DEFAULT_CFG = {
    "segments": [
        {"name": "long", "label": "长期", "train": [20, 5], "test": [5, 0], "min_pf": None},
        {"name": "medium", "label": "中期", "train": [5, 1.5], "test": [1.5, 0], "min_pf": None},
        {"name": "short", "label": "短期", "train": [2, 0.5], "test": [0.5, 0], "min_pf": None},
    ],
    "default_pf": 1.0, "min_seg_trades": 10, "batch_limit": 50,
}


def _p(**over):
    return cfg_params({**DEFAULT_CFG, **over})


def _ts(years_ago: float) -> float:
    """距锚点 years_ago 年(按定版 365.25 天/年)的时间戳"""
    return (anchor_dt(ANCHOR) - timedelta(days=years_ago * YEAR_DAYS)).timestamp()


def _trade(years_ago: float, points: float) -> dict:
    return {"entry_time": _ts(years_ago), "points": points}


# ---------- 切点时间学(定版三约定: UTC 0点 / 365.25天 / 左闭右开) ----------

def test_anchor_is_utc_midnight():
    a = anchor_dt(ANCHOR)
    assert (a.hour, a.minute, a.second, a.tzinfo) == (0, 0, 0, timezone.utc)


def test_segment1_boundary_locked():
    """段1 严格 = A−20y → A−5y(2026-08-06 Frank 特别强调, 不是"前15年"另算法)"""
    t0, t1 = seg_window(anchor_dt(ANCHOR), [20, 5])
    assert t0 == pytest.approx((anchor_dt(ANCHOR) - timedelta(days=20 * 365.25)).timestamp())
    assert t1 == pytest.approx((anchor_dt(ANCHOR) - timedelta(days=5 * 365.25)).timestamp())


def test_boundary_half_open():
    """左闭右开 [起, 止): 刚好在切点上的笔归后段, 不重复不遗漏"""
    p = _p()
    cut = _ts(5)  # 长训|长测 的界
    trades = [{"entry_time": cut, "points": 100},          # 正好切点 → 归长测(右段)
              {"entry_time": cut - 1, "points": -50}]      # 切点前 1 秒 → 归长训
    r = judge_trades(trades, ANCHOR, p)
    long = r["periods"][0]
    assert long["train"]["n"] == 1 and long["train"]["net"] == -50.0
    assert long["test"]["n"] == 1 and long["test"]["net"] == 100.0


def test_overlap_by_design():
    """段与段有意重叠: 近 1 年的一笔同时落在 长测/中测/短训+短测 之外的正确段组合"""
    r = judge_trades([_trade(0.3, 10)], ANCHOR, _p())
    seat = {(per["name"], part): per[part]["n"]
            for per in r["periods"] for part in ("train", "test")}
    assert seat == {("long", "train"): 0, ("long", "test"): 1,
                    ("medium", "train"): 0, ("medium", "test"): 1,
                    ("short", "train"): 0, ("short", "test"): 1}


def test_window_years_is_max_of_segments():
    assert window_years(_p()) == 20
    p = _p(segments=DEFAULT_CFG["segments"]
           + [{"name": "x", "label": "超长", "train": [30, 10], "test": [10, 0]}])
    assert window_years(p) == 30


# ---------- PF 三边界(定版: >门槛=过, ∞恒过, 0笔不追责算过; 全亏 PF=0 红) ----------

def _fill(pos=6, neg=6):
    """往六段各塞 pos 笔赚/neg 笔亏(PF=2 全过的基准形状): 每段中点附近落笔"""
    mids = [12.5, 2.5, 3.2, 0.75, 1.7, 0.25]   # 长训/长测/中训/中测/短训/短测 段内点位
    out = []
    for m in mids:
        out += [_trade(m + i * 0.01, 100) for i in range(pos)]
        out += [_trade(m - 0.05 - i * 0.01, -50) for i in range(neg)]
    return out


def test_all_green_pass():
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": _fill()}, ANCHOR, _p())
    assert d["verdict"] == "pass" and d["reason"].startswith("全段合格")
    assert all(per[part]["pf"] == pytest.approx(2.0)
               for per in d["periods"] for part in ("train", "test"))


def test_one_red_fails():
    trades = _fill() + [_trade(0.25, -10000)]   # 短测段砸一笔大亏 → 该段 PF<1
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": trades}, ANCHOR, _p())
    st = d["periods"][2]["test"]
    assert st["ok"] is False and st["pf"] < 1
    assert d["verdict"] == "fail" and "短期测试" in d["reason"]


def test_empty_segment_counts_as_pass_with_warn():
    """0 笔段: 无数据不追责 → 算过(pf=None 显 —), 但挂样本不足警示; 不阻止 PASS"""
    trades = [t for t in _fill() if t["entry_time"] >= _ts(5)]   # 抽空长训(>5年前)
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": trades}, ANCHOR, _p())
    lt = d["periods"][0]["train"]
    assert lt["n"] == 0 and lt["pf"] is None and lt["inf"] is False
    assert lt["ok"] is True and lt["warn"] is True
    assert d["verdict"] == "pass" and d["warn"] is True and "样本不足" in d["reason"]


def test_no_loss_segment_is_inf_pass():
    """无亏损段(有笔): 引擎 profit_factor=None → ∞ 恒过, inf 标记与 0 笔的 — 区分"""
    trades = [t for t in _fill() if not (t["points"] < 0 and _ts(5) > t["entry_time"])]
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": trades}, ANCHOR, _p())
    lt = d["periods"][0]["train"]
    assert lt["n"] == 6 and lt["pf"] is None and lt["inf"] is True and lt["ok"] is True
    assert d["verdict"] == "pass"


def test_all_loss_segment_pf_zero_fails():
    trades = [t for t in _fill() if not (t["points"] > 0 and t["entry_time"] < _ts(5))]
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": trades}, ANCHOR, _p())
    lt = d["periods"][0]["train"]
    assert lt["pf"] == 0 and lt["ok"] is False and d["verdict"] == "fail"


def test_warn_never_flips_verdict():
    """样本不足只警示不判定: 每段 1 笔赚(全部 <10 笔) → 照样 PASS + warn"""
    trades = [_trade(m, 100) for m in (12.5, 2.5, 3.2, 0.75, 1.7, 0.25)]
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": trades}, ANCHOR, _p())
    assert d["verdict"] == "pass" and d["warn"] is True


# ---------- 两层 PF 门槛(每期 min_pf 覆盖, null 回落 default_pf) ----------

def test_per_segment_pf_override():
    """全段 PF=2: 默认门槛 1 全过; 把短期门槛提到 3 → 只有短期两段红"""
    segs = [dict(s) for s in DEFAULT_CFG["segments"]]
    segs[2] = {**segs[2], "min_pf": 3.0}
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": _fill()}, ANCHOR, _p(segments=segs))
    assert [per["min_pf"] for per in d["periods"]] == [1.0, 1.0, 3.0]
    assert d["periods"][0]["train"]["ok"] and d["periods"][1]["test"]["ok"]
    assert not d["periods"][2]["train"]["ok"] and not d["periods"][2]["test"]["ok"]
    assert d["verdict"] == "fail"


def test_default_pf_fallback():
    """default_pf 提到 2.5, 各期 min_pf=null → 全跟着 2.5(PF=2 全红)"""
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": _fill()}, ANCHOR, _p(default_pf=2.5))
    assert all(per["min_pf"] == 2.5 for per in d["periods"])
    assert d["verdict"] == "fail"


def test_pf_threshold_is_strict_gt():
    """门槛是严格大于: PF 恰好 = 门槛 → 红(毛利600/毛损300=2.0, 门槛2.0)"""
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"trades": _fill()}, ANCHOR, _p(default_pf=2.0))
    assert d["verdict"] == "fail"


# ---------- 铁则1: 缺数据永不淘汰 ----------

def test_missing_backtest_row_skips():
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  None, ANCHOR, _p())
    assert d["verdict"] == "skip" and "缺回测行" in d["reason"]


def test_failed_job_skips():
    d = judge_one({"id": 1, "name": "s", "symbol": "EURUSD", "status": "CANDIDATE"},
                  {"error": "no M1 data"}, ANCHOR, _p())
    assert d["verdict"] == "skip" and "回测失败" in d["reason"]


def test_summarize_counts():
    details = [{"verdict": "pass", "warn": True}, {"verdict": "pass"},
               {"verdict": "fail"}, {"verdict": "skip"}]
    s = summarize(details, "preview", archived=9, not_run=3)
    assert s == {"total": 4, "passed": 2, "failed": 1, "skipped": 1,
                 "warned": 1, "archived": 0, "not_run": 3}
    assert summarize(details, "execute", archived=9, not_run=0)["archived"] == 9


# ---------- 配置校验(非法保存被拒) ----------

@pytest.mark.parametrize("bad, msg", [
    ({"segments": []}, "不能为空"),
    ({"segments": [{"name": "a", "train": [2, 5], "test": [1, 0]}]}, "必须大于"),   # 起<止
    ({"segments": [{"name": "a", "train": [5, 5], "test": [1, 0]}]}, "必须大于"),   # 起=止
    ({"segments": [{"name": "a", "train": [5, -1], "test": [1, 0]}]}, "必须大于"),  # 止<0
    ({"segments": [{"name": "a", "train": [5, 1], "test": "x"}]}, "须为"),
    ({"segments": [{"name": "a", "train": [5, "y"], "test": [1, 0]}]}, "数字"),
    ({"segments": [{"name": "", "train": [5, 1], "test": [1, 0]}]}, "缺 name"),
    ({"segments": [{"name": "a", "train": [5, 1], "test": [1, 0]},
                   {"name": "a", "train": [3, 1], "test": [1, 0]}]}, "重复"),
])
def test_invalid_config_rejected(bad, msg):
    with pytest.raises(ValueError, match=msg):
        cfg_params({**DEFAULT_CFG, **bad})


def test_config_defaults_and_min_pf_empty_string():
    """页面提交空字符串 = null(用默认); 缺省键回默认"""
    p = cfg_params({"segments": [{"name": "a", "train": [5, 1],
                                  "test": [1, 0], "min_pf": ""}]})
    assert p["segments"][0]["min_pf"] is None
    assert (p["default_pf"], p["min_seg_trades"], p["batch_limit"]) == (1.0, 10, 50)
    assert "reuse_days" not in p     # 复用 = 全局 backtest_reuse_days, 不在本模块判据里
    assert "judge_chunk" not in p    # 判定块 = 全局 judge_chunk(schema/069), 同上
