"""Regime 门(v0.3 定稿): metadata 裁剪执行 — 回测引擎与实盘 runner 共用这一份逻辑。

纯函数零状态: 输入 metadata + 当日格子 → 手数倍率(None=不开新仓)。
两端一致性由"同一个函数"保证, 不是"两段相似代码":
  回测: 入场 bar 的券商日期查该版本时间线得格 → gate_mult → None跳过 / 倍率加权净点
  实盘: api 随策略下发当日格(钉死版本) → gate_mult → None跳过 / 手数×倍率下单
metadata 校验在收货管道(唯一写入口), 这里只做形状判断 — 进了库的门必然合法。
"""


def regime_gate(metadata) -> dict | None:
    """metadata → 门配置 {"version": int, "cells": {格: 倍率}}; 无门(空/{}) → None"""
    g = metadata.get("regime") if isinstance(metadata, dict) else None
    if isinstance(g, dict) and isinstance(g.get("cells"), dict) and g["cells"]:
        return g
    return None


def gate_mult(gate: dict | None, cell: str | None) -> float | None:
    """入场裁决(两端同一函数):
    无门 → 1.0(全量);
    有门: 格在 cells → 该格倍率; 格不在 / 当日无格(时间线缺行) → None = 不开新仓
    (悲观方向: 不知道天气就不交易)。已有持仓不受影响(入场门, 非强平门)。"""
    if gate is None:
        return 1.0
    if cell is None:
        return None
    m = gate["cells"].get(cell)
    return float(m) if m is not None else None
