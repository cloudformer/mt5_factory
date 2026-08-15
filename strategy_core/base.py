"""策略基类: 回测(Linux app)与实时runner(Windows)共用同一份代码, 只换数据源/执行器

纪律(CLAUDE.md):
- 只在已收盘 bar 上决策
- 每个信号必带 SL/TP(绝对价格), 下单落到券商服务端
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Signal:
    direction: str  # "BUY" | "SELL"
    sl: float       # 止损绝对价 (必填)
    tp: float       # 止盈绝对价 (必填)


class Strategy(ABC):
    """bar 级策略模板基类

    on_bar 输入: 最近 warmup 根已收盘 bar 的 numpy 数组(旧→新)
    on_bar 输出: 开仓信号或 None (仅在空仓时被调用; 离场只靠 SL/TP)

    self.t (2026-08-15 附加约定): 调用方(引擎/runner)在每次 on_bar 前把与 o/h/l/c 对齐的
    bar 时间戳数组(epoch 秒, 券商服务器时间)挂在 self.t 上 — 时段类策略(开盘区间/伦敦时段)
    用它取钟点; 不需要的模板直接无视, 行为不变。两端同一约定, 回测/实盘不分叉。
    """
    t = None   # 由调用方在 on_bar 前注入; 模板只读
    PARAM_GRID: dict = {}    # 网格生成: 固定组合, 有限空间
    RANDOM_SPACE: dict = {}  # 随机生成: {param: (min, max, step)}, 近乎无限空间

    def __init__(self, params: dict, point: float):
        self.params = params
        self.point = point

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        """过滤无意义的参数组合"""
        return True

    @property
    @abstractmethod
    def warmup(self) -> int:
        """决策所需的最少已收盘 bar 数"""

    @abstractmethod
    def on_bar(self, o, h, l, c) -> Optional[Signal]:
        ...
