-- 072_users_license.sql — 用户授权纸(license 第3步, 2026-08-14 与 Frank 定)
-- 一列装整份签名文档 {license:{user_id,name,expires,workers,max_strategies}, sig}。
-- 库只是放纸的抽屉不是权威: api 每次读出都用 git 里的公钥重验签名(license/canonical.py),
-- 改库里任何一个字 → 验签失败 → 按无效禁用(不是回落"不限" — 篡改只能把自己搞停)。
-- NULL = 无授权限制(owner 与存量用户零影响, license 是逐用户 opt-in)。
-- worker 没有自己的纸: 钥匙(worker_keys)只证明身份, 权利全部从本列继承。
ALTER TABLE users ADD COLUMN IF NOT EXISTS license TEXT;
