"""今日格性格卡(regime.cell_character)不变量: 纯描述历史 — 当前未完段不进统计;
频率分母 = 该格历史完结段数; 排序 = 频次降序(平频按格名)。"""
from src.services.regime import cell_character


def test_empty():
    assert cell_character([]) == {}


def test_current_run_excluded_from_history():
    # A×2, B×1, A×3(当前段): 历史 A 段只有第一段(2天); 转移 A→B 1次(100%)
    r = cell_character(["AAA", "AAA", "BBB", "AAA", "AAA", "AAA"])
    assert r["cell"] == "AAA" and r["run_days"] == 3
    assert r["runs"] == 1 and r["avg_days"] == 2.0 and r["max_days"] == 2
    assert r["next"] == [{"cell": "BBB", "n": 1, "pct": 100}]


def test_transition_frequencies():
    # A→B ×2, A→C ×1; 当前段是 A(未完) → 3 段历史 A, 转移分母 3
    seq = (["AAA"] * 2 + ["BBB"] + ["AAA"] * 4 + ["CCC"]
           + ["AAA"] * 3 + ["BBB"] + ["AAA"])
    r = cell_character(seq)
    assert r["runs"] == 3
    assert r["avg_days"] == 3.0 and r["max_days"] == 4     # 历史段 2/4/3 天
    assert r["next"][0] == {"cell": "BBB", "n": 2, "pct": 67}
    assert r["next"][1] == {"cell": "CCC", "n": 1, "pct": 33}


def test_first_ever_cell_has_no_history():
    r = cell_character(["AAB", "AAB"])
    assert r["runs"] == 0 and r["avg_days"] is None and r["next"] == []
