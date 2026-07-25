-- 040_worker_keys.sql — worker 钥匙(2026-07-25 与 Frank 定): 发一把钥匙即扩容
-- 流程: 管理页给用户签发 → 写进 Windows env(WORKER_KEY) → 机器 announce 带钥 →
--       api 认钥知主 → mt5_hosts 自动归该用户 + 钥匙与这台机器首次绑定(一机一钥)。
-- 与 api_keys(用户浏览/脚本钥匙)分表: worker 是真金通道, 语义含"绑定机器", 分开管分开吊。
-- 方向: 只验 Windows→Linux; api→bridge 方向保留 BRIDGE_API_KEY(库里只有哈希拿不出明文)。
-- 本步无人强制(认钥归属=增强, 不带钥照旧); 强制在 v5.6。通信单向化展望见 v7.2。
CREATE TABLE IF NOT EXISTS worker_keys (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),   -- 谁的钥匙(注册即归他)
    key_hash     TEXT NOT NULL UNIQUE,                    -- 只存 sha256, 明文签发时一次
    name         VARCHAR(64),                             -- 备注: 打算给哪台机
    host_id      INTEGER UNIQUE REFERENCES mt5_hosts(id), -- 首次注册自动绑定; UNIQUE=一机一钥
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,           -- 吊销时自动解绑(轮换=吊旧发新)
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
