"""parse_ids 契约回归(2026-08-15 Frank 定的全站格式): 规范输出 = 1,2,3;
输入宽容 — 方括号/空格/分号/中文逗号/换行都认, 真垃圾抛 ValueError。
五个页面(回测/OOS/regime筛选/规律/预测)共用这一个函数, 改坏 = 全站按ID同时坏。"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("requests")                      # api_client 顶层依赖; 没装则跳过
os.environ.setdefault("API_URL", "http://test:0")   # api_client 导入时校验必有, 测试给假值
_p = Path(__file__).resolve().parents[3] / "containers" / "web" / "api_client.py"
_spec = importlib.util.spec_from_file_location("_api_client", _p)
_m = importlib.util.module_from_spec(_spec)
sys.modules["_api_client"] = _m
_spec.loader.exec_module(_m)
parse_ids = _m.parse_ids


@pytest.mark.parametrize("raw,expect", [
    ("1,2,3", [1, 2, 3]),
    ("[1, 2, 3]", [1, 2, 3]),          # 旧报告复制的 JSON 也认
    ("1，2，3", [1, 2, 3]),             # 中文逗号
    ("1 2 3", [1, 2, 3]),
    ("1;2;3", [1, 2, 3]),
    ("11745,\n11746", [11745, 11746]),  # 换行
    ("", []),
    ("  ", []),
])
def test_tolerant_inputs(raw, expect):
    assert parse_ids(raw) == expect


def test_garbage_raises():
    with pytest.raises(ValueError):
        parse_ids("1,abc,3")
