"""pytest 路径接线: 让测试能 import 生产代码, 不改任何源文件。
  src.*          → containers/api 在 path
  strategy_core  → repo 根在 path
运行: cd 到 repo 根, `pytest containers/api/tests`  (或 `make test-engine`)。"""
import sys
from pathlib import Path

_api = Path(__file__).resolve().parents[1]      # containers/api
_root = Path(__file__).resolve().parents[3]     # repo 根
for p in (str(_api), str(_root)):
    if p not in sys.path:
        sys.path.insert(0, p)
