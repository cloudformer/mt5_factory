"""regime 底色铺段回归(2026-08-18): 纯函数 band_segments 的不变量与边界。
规则: 格顺延到下一交易日(周末不留白); >4天洞留白; 同格相邻合并。改铺段逻辑前后必跑。"""
from datetime import date, timedelta

from src.routes.data import band_segments

D0 = date(2026, 8, 10)   # 周一


def mk(cells):
    """从周一起按序贴格, None=跳过该日(休市/缺数据)"""
    return {D0 + timedelta(days=i): c for i, c in enumerate(cells) if c}


def _invariants(segs):
    for s in segs:
        assert s[1] > s[0], "段内 end 必须 > start"
    for a, b in zip(segs, segs[1:]):
        assert b[0] >= a[1], "相邻段不得重叠"
    assert segs == sorted(segs, key=lambda s: s[0]), "段起点必须单调递增"


def test_weekend_bridged_same_cell():
    # 周一~周五 AAB, 周末无行, 下周一 AAB → 合成一段, 周末不留白
    tl = mk(["AAB"] * 5 + [None, None] + ["AAB"])
    segs = band_segments(tl)
    _invariants(segs)
    assert len(segs) == 1
    assert (segs[0][1] - segs[0][0]) == 86400 * 8   # 周一0点 → 下周二0点


def test_weekend_bridged_cell_change():
    # 周五 AAB → 下周一 ABA: 周末归周五的格(顺延), 两段首尾相接零空隙
    tl = mk(["AAB"] * 5 + [None, None] + ["ABA"])
    segs = band_segments(tl)
    _invariants(segs)
    assert len(segs) == 2
    assert segs[0][1] == segs[1][0], "跨周末换格不得留白"
    assert [s[2] for s in segs] == ["AAB", "ABA"]


def test_long_hole_stays_white():
    # 缺 6 天(真数据洞): 前段最多顺延 4 天, 洞如实留白
    tl = mk(["AAB"] + [None] * 6 + ["AAB"])
    segs = band_segments(tl)
    _invariants(segs)
    assert len(segs) == 2, ">4天洞不得桥接(哪怕同格)"
    assert segs[0][1] == segs[0][0] + 86400 * 4
    assert segs[1][0] - segs[0][1] == 86400 * 3     # 剩 3 天白 = 洞的真实长度


def test_single_day_and_empty():
    assert band_segments({}) == []
    segs = band_segments(mk(["BBB"]))
    _invariants(segs)
    assert segs == [[segs[0][0], segs[0][0] + 86400, "BBB"]]


def test_alternating_cells_daily():
    # 逐日换格: 每天一段, 全部首尾相接
    tl = mk(["AAA", "BBB", "AAA", "BBB"])
    segs = band_segments(tl)
    _invariants(segs)
    assert len(segs) == 4
    assert all(b[0] == a[1] for a, b in zip(segs, segs[1:]))
