"""用户授权(license 第3步, 2026-08-14) — 验签与状态判读, 零执法(执法在第4步逐个拧开)。

纸的唯一权威 = 签名(license/canonical.py, 与签发脚本共用同一份 canonical/verify);
库(users.license)只是抽屉。状态口径:
  none        没纸 = 无授权限制(owner/存量用户, opt-in 原则)
  valid       签名过 + 未到期
  expired     签名过 + 已到期
  invalid     有纸但验签失败/格式坏/user_id 对不上 — 按禁用处理, 绝不回落"不限"
  unavailable 系统公钥还是占位符(keygen 未跑) — 一切按 none, 但页面亮黄提示
"""
import json
import logging
from datetime import datetime, timezone

from license.canonical import PUBLIC_KEY_B64, verify

logger = logging.getLogger("license")

KEY_READY = not PUBLIC_KEY_B64.startswith("REPLACE_ME")


def parse(raw: str | None, expect_user_id: int) -> dict:
    """users.license 原文 → 状态字典(每次现验, 用户表就几行, 微秒级不值得缓存)。
    expect_user_id: 纸里的 user_id 必须等于它 — 把 3 号的纸贴到 5 号头上不算数。"""
    if not raw:
        return {"status": "none"}
    if not KEY_READY:
        return {"status": "unavailable",
                "error": "系统公钥未生成(scripts/make_license.py keygen), 暂按无授权处理"}
    try:
        payload = verify(json.loads(raw))
    except (ValueError, json.JSONDecodeError) as e:
        return {"status": "invalid", "error": str(e)}
    if payload["user_id"] != expect_user_id:
        return {"status": "invalid",
                "error": f"纸是 user_id={payload['user_id']} 的, 贴错了人"}
    try:
        exp = datetime.fromisoformat(payload["expires"])
    except ValueError:
        return {"status": "invalid", "error": f"到期日格式坏: {payload['expires']!r}"}
    out = {"expires": payload["expires"], "workers": payload["workers"],
           "max_strategies": payload["max_strategies"], "name": payload["name"]}
    left = exp - datetime.now(timezone.utc)
    if left.total_seconds() <= 0:
        return {**out, "status": "expired", "days_left": left.days}
    return {**out, "status": "valid", "days_left": left.days}
