#!/usr/bin/env python3
"""license 签发工具(2026-08-14 与 Frank 定) — 只在 Frank 本机跑, 私钥永不进 git。

用法:
  python scripts/make_license.py keygen                     # 一次性: 生成密钥对
  python scripts/make_license.py sign --user-id 3 --name acme \\
      --expires 2026-09-09 --workers 2 --max-strategies 100   # 签发(输出整份 JSON)
  python scripts/make_license.py verify license.json          # 验一份(与 api 同一套验签)

私钥固定存 ~/.mt5_factory/license_signing.key(路径写死在家目录 = 构造上在仓库外);
公钥打进 license/canonical.py 的 PUBLIC_KEY_B64(keygen 会自动替换)。
"""
import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from license.canonical import canonical, verify, PUBLIC_KEY_B64  # noqa: E402

KEY_FILE = Path.home() / ".mt5_factory" / "license_signing.key"
CANONICAL_PY = Path(__file__).resolve().parents[1] / "license" / "canonical.py"


def keygen(args):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    if KEY_FILE.exists() and not args.force:
        sys.exit(f"私钥已存在: {KEY_FILE} (重新生成会作废所有在发 license, 确要重来加 --force)")
    priv = Ed25519PrivateKey.generate()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()))
    KEY_FILE.chmod(0o600)
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    src = CANONICAL_PY.read_text(encoding="utf-8")
    import re
    src = re.sub(r'PUBLIC_KEY_B64 = "[^"]*"', f'PUBLIC_KEY_B64 = "{pub_b64}"', src)
    CANONICAL_PY.write_text(src, encoding="utf-8")
    print(f"私钥 → {KEY_FILE} (0600, 备份它! 丢了 = 所有 license 要重签)")
    print(f"公钥 → 已写进 {CANONICAL_PY.name}: {pub_b64}")


def sign(args):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if not KEY_FILE.exists():
        sys.exit(f"没有私钥: 先跑 keygen (找不到 {KEY_FILE})")
    # 到期日: 只给日期则按当天 23:59:59 UTC(给整天, 不在中午掐断)
    exp = args.expires if "T" in args.expires else f"{args.expires}T23:59:59+00:00"
    datetime.fromisoformat(exp)   # 早失败: 格式不对就别签出去
    payload = {"user_id": args.user_id, "name": args.name, "expires": exp,
               "workers": args.workers, "max_strategies": args.max_strategies}
    priv = Ed25519PrivateKey.from_private_bytes(KEY_FILE.read_bytes())
    doc = {"license": payload,
           "sig": base64.b64encode(priv.sign(canonical(payload))).decode()}
    out = json.dumps(doc, ensure_ascii=False, indent=2)
    verify(doc)   # 自检: 签出去前用公钥(git里那份)验一遍, 钥匙不配套当场发现
    print(out)


def do_verify(args):
    doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
    payload = verify(doc)
    exp = datetime.fromisoformat(payload["expires"])
    left = exp - datetime.now(timezone.utc)
    state = f"有效, 剩 {left.days} 天" if left.total_seconds() > 0 else f"已过期 {-left.days} 天"
    print(f"签名 OK · user_id={payload['user_id']} name={payload['name']}"
          f" · workers={payload['workers']} · max_strategies={payload['max_strategies']}"
          f" · 到期 {payload['expires']} ({state})")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--force", action="store_true")
    s = sub.add_parser("sign")
    s.add_argument("--user-id", type=int, required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--expires", required=True, help="YYYY-MM-DD 或完整 ISO 时间")
    s.add_argument("--workers", type=int, required=True)
    s.add_argument("--max-strategies", type=int, required=True)
    v = sub.add_parser("verify"); v.add_argument("file")
    args = ap.parse_args()
    {"keygen": keygen, "sign": sign, "verify": do_verify}[args.cmd](args)


if __name__ == "__main__":
    main()
