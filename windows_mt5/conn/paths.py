"""定位 repo 根和共享 env 文件 —— 向上找标志文件, 不用硬编码层级深度。

以前每个脚本写 Path(__file__).parents[2] 这种魔法数字, 绑死了文件位置, 一挪就静默失效。
这里从本文件向上逐级找 env/.dev.env.example (repo 里稳定存在的标志), 谁调用、在第几层都对。
以后新脚本读 env 只需:  from conn.paths import ENV_FILE, REPO_ROOT
"""
from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "env" / ".dev.env.example").exists():
            return parent
    raise RuntimeError("repo root not found: no env/.dev.env.example above conn/")


REPO_ROOT = _repo_root()
ENV_FILE = REPO_ROOT / "env" / ".dev.env"
