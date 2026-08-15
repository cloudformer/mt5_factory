"""license 签名契约回归(2026-08-15): canonical 字节唯一性 + 验签/篡改/贴错人。
这是授权体系的地基 — canonical() 两边共用一份, 但谁改了序列化规则(键序/空格/转义)
所有已签发的纸就集体失效; 这张网让那种改动当场红灯。
cryptography 未装时跳过(生产镜像有, 本地精简环境可能没有)。"""
import base64
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # 仓库根: license/ 在那
from license import canonical as canon                          # noqa: E402

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

PAYLOAD = {"user_id": 3, "name": "acme", "expires": "2026-09-09T23:59:59+00:00",
           "workers": 2, "max_strategies": 100}


def test_canonical_is_byte_stable():
    # 键序打乱/重复调用 → 字节必须一致(签名盖的是字节, 序列化规则漂移=所有纸失效)
    shuffled = {k: PAYLOAD[k] for k in reversed(list(PAYLOAD))}
    assert canon.canonical(PAYLOAD) == canon.canonical(shuffled)
    assert canon.canonical(PAYLOAD) == canon.canonical(json.loads(json.dumps(PAYLOAD)))


@pytest.fixture()
def keypair(monkeypatch):
    """临时密钥对 + 把公钥打进模块(不碰 repo 里的常量)"""
    priv = Ed25519PrivateKey.generate()
    pub = base64.b64encode(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    monkeypatch.setattr(canon, "PUBLIC_KEY_B64", pub)
    return priv


def sign(priv, payload):
    return {"license": payload,
            "sig": base64.b64encode(priv.sign(canon.canonical(payload))).decode()}


def test_verify_roundtrip(keypair):
    assert canon.verify(sign(keypair, PAYLOAD)) == PAYLOAD


def test_tamper_one_field_fails(keypair):
    doc = sign(keypair, PAYLOAD)
    doc["license"] = {**PAYLOAD, "workers": 9}      # 2台改9台
    with pytest.raises(ValueError, match="签名无效"):
        canon.verify(doc)


def test_missing_field_fails(keypair):
    bad = {k: v for k, v in PAYLOAD.items() if k != "expires"}
    with pytest.raises(ValueError, match="缺字段"):
        canon.verify(sign(keypair, bad))


def test_wrong_key_fails(keypair):
    other = Ed25519PrivateKey.generate()             # 别人的私钥签的
    with pytest.raises(ValueError, match="签名无效"):
        canon.verify(sign(other, PAYLOAD))
