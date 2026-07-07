"""MQ5 翻译一致性验证

原理: 同品种同时段, 原版 EA 在 MT5 Strategy Tester 跑出的入场记录 (用户粘贴报告)
     vs 翻译模板在我们回测引擎跑出的入场记录 — 按 "bar桶+方向" 比对, 出一致率%。

一致率 = 双方都出现的入场 / 双方中较多的一方 (100% = 每笔信号时间和方向都对上)。
"""
import re
from collections import Counter
from datetime import datetime, timezone

_DT = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?")


def parse_tester_deals(text: str) -> list[tuple[int, str]]:
    """解析 MT5 回测报告 Deals 表的粘贴文本, 提取入场记录 [(epoch秒, 'BUY'|'SELL')]。
    兼容制表符/多空格分隔; 只取 direction 为 in 的行 (入场), 忽略出场和汇总行。"""
    entries = []
    for line in text.splitlines():
        m = _DT.search(line)
        if not m:
            continue
        tokens = [t.strip().lower() for t in re.split(r"\t|\s{2,}", line) if t.strip()]
        if "in" not in tokens:
            continue
        side = "BUY" if "buy" in tokens else ("SELL" if "sell" in tokens else None)
        if side is None:
            continue
        y, mo, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
        epoch = int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())
        entries.append((epoch, side))
    return entries


def compare_entries(ours: list[tuple[int, str]], original: list[tuple[int, str]],
                    tf_seconds: int) -> dict:
    """按 TF bar 桶 + 方向比对两组入场"""
    a = Counter((t // tf_seconds, d) for t, d in ours)
    b = Counter((t // tf_seconds, d) for t, d in original)
    matched = sum((a & b).values())
    denom = max(sum(a.values()), sum(b.values())) or 1
    return {
        "consistency": round(100.0 * matched / denom, 1),
        "matched": matched,
        "ours": sum(a.values()),
        "original": sum(b.values()),
    }
