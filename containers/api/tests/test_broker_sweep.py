"""v2.3 户口制零残留哨兵: 数据表查询必须带 broker 维度。

historical_bars / regime_timeline 的主键都含 broker — 任何不带 broker 的查询
在多券商时代都是潜在串数据(同名品种两家券商的行混在一起)。本测试把施工期的
"零残留清查 grep" 固化下来: 新增查询忘带 broker 直接红灯, 不等上线才发现。

判定口径与清查一致: 命中行前2行~后4行的窗口内必须出现 "broker" 字样
(SQL 拼接跨行, 条件通常紧跟在 FROM 之后)。
"""
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
TABLES = ("FROM historical_bars", "FROM regime_timeline")


def _violations() -> list[str]:
    out = []
    for py in sorted(SRC.rglob("*.py")):
        lines = py.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not any(t in line for t in TABLES):
                continue
            window = "\n".join(lines[max(0, i - 2): i + 5])
            if "broker" not in window:
                out.append(f"{py.relative_to(SRC)}:{i + 1}: {line.strip()[:90]}")
    return out


def test_bars_and_timeline_queries_carry_broker():
    v = _violations()
    assert not v, "以下查询没带 broker 维度(v2.3 户口制违规):\n" + "\n".join(v)
