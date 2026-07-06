"""strategy_core: 回测与实时执行共用的策略包"""
import itertools
import random

from .base import Signal, Strategy
from .templates import TEMPLATES

TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}


def make_strategy(template: str, params: dict, point: float) -> Strategy:
    return TEMPLATES[template](params, point)


def grid_combos(template: str) -> list[dict]:
    """展开模板参数网格, 过滤无效组合"""
    cls = TEMPLATES[template]
    keys = list(cls.PARAM_GRID)
    combos = []
    for values in itertools.product(*(cls.PARAM_GRID[k] for k in keys)):
        params = dict(zip(keys, values))
        if cls.valid_params(params):
            combos.append(params)
    return combos


def random_combo(template: str, rng: random.Random) -> dict | None:
    """在 RANDOM_SPACE 范围内随机采样一组参数 (按 step 取整对齐)"""
    cls = TEMPLATES[template]
    for _ in range(50):  # 最多尝试50次找到有效组合
        params = {}
        for key, (lo, hi, step) in cls.RANDOM_SPACE.items():
            value = lo + rng.randint(0, int(round((hi - lo) / step))) * step
            params[key] = round(value, 4) if isinstance(step, float) else int(value)
        if cls.valid_params(params):
            return params
    return None
