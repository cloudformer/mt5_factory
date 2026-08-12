"""预测验证(v0.7 批次3)判定回归网: 成熟度门槛 / 三档结论 / PF∞ 边界 — 口径测试锁死。"""
import pytest

from src.services.prediction import cfg_params, judge

P = cfg_params({})   # 默认: 3年窗 / 20笔 / 90天 / 保持率0.8


def _a(n=50, pf=1.2):
    return {"n": n, "pf": pf, "net": 100.0, "dd": 50.0, "win_rate": 0.5}


def test_immature_by_trades():
    r = judge(1.5, _a(n=5), None, days=200, p=P)
    assert r["verdict"] == "immature" and "5 笔" in r["reason"]


def test_immature_by_days():
    r = judge(1.5, _a(n=50), None, days=30, p=P)
    assert r["verdict"] == "immature" and "30 天" in r["reason"]


def test_frank_example_1p5_to_1p2_is_valid():
    """Frank 定版用例: 期望1.5 → 实际1.2 = 保持率0.80 恰好及格 → 有效"""
    r = judge(1.5, _a(pf=1.2), baseline_pf=0.9, days=200, p=P)
    assert r["verdict"] == "valid"
    assert r["retention"] == pytest.approx(0.8)
    assert r["gain"] == pytest.approx(1.333, abs=1e-3)   # 门内1.2 vs 无门0.9 = 门救命


def test_decayed():
    r = judge(1.5, _a(pf=1.1), None, days=200, p=P)
    assert r["verdict"] == "decayed" and r["retention"] == pytest.approx(0.733, abs=1e-3)


def test_broken_pf_below_1():
    r = judge(1.5, _a(pf=0.9), 1.0, days=200, p=P)
    assert r["verdict"] == "broken"


def test_actual_inf_is_valid():
    r = judge(1.5, _a(pf=None), None, days=200, p=P)   # 有笔无亏损 = ∞
    assert r["verdict"] == "valid"


def test_expected_inf_judged_by_actual():
    r = judge(None, _a(pf=1.3), None, days=200, p=P)   # 期望∞: 保持率无定义, actual>1 即有效
    assert r["verdict"] == "valid" and r["retention"] is None


def test_config_defaults():
    assert P == {"expected_window_years": 3.0, "min_trades": 20,
                 "min_days": 90, "retention_ok": 0.8,
                 "stability_batch": 30}   # 批次序列版每批笔数(c563c21 加, 页面可调不落库)
