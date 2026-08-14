"""license 规范化与验签 — 唯一实现, 签发脚本与 api 共 import 这一份。

为什么必须共享一份(2026-08-14 与 Frank 定): 签名盖住的是"字节"不是"意思",
同一个 JSON 有无数种写法(键序/空格/转义), 序列化规则在两处各写一遍迟早分叉 —
canonical() 只此一份, 签发和验证在字节层面天然一致。

放在仓库根的 license/ 而不是 containers/api/ 里: api 与签发脚本(scripts/)都要用,
未来若做 worker 本地验签(自托管部署)也直接 import 同一份。
"""
import base64
import json

# Ed25519 公钥(2026-08-14 生成, 私钥只在 Frank 机器上, 不进 git)。
# 换钥匙 = 改这一行 + 重签所有在发的 license。
PUBLIC_KEY_B64 = "REPLACE_ME_AFTER_KEYGEN"


def canonical(payload: dict) -> bytes:
    """license 字典 → 唯一字节串(键排序/无空格/不转义中文)。签名与验签都只认它。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def verify(doc: dict) -> dict:
    """验签: {license: {...}, sig: b64} → 通过返回 license 字典, 不通过抛 ValueError。

    库(users.license 列)只是放纸的抽屉, 不是权威 — 每次读出都走这里重验,
    库里任何一个字被改过, 字节就变了, 签名立刻对不上。
    「有纸但签名无效」≠「没纸」: 前者按无效禁用, 后者不限 — 篡改只能把自己搞停。
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    if not isinstance(doc, dict) or "license" not in doc or "sig" not in doc:
        raise ValueError("格式不对: 需要 {license: {...}, sig: base64}")
    payload = doc["license"]
    required = {"user_id", "name", "expires", "workers", "max_strategies"}
    missing = required - set(payload)
    if missing:
        raise ValueError(f"license 缺字段: {', '.join(sorted(missing))}")
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
    try:
        pub.verify(base64.b64decode(doc["sig"]), canonical(payload))
    except InvalidSignature:
        raise ValueError("签名无效 — 内容被改过, 或不是本系统私钥签发的")
    return payload
