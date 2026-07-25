-- 034_user_auth_worker_owner.sql — v5.2 用户体系(认证信息+key+配置覆盖层) + worker 归属(2026-07-24)
-- 只建表打标, 不接认证(认证中间件=行为变更, 属 v5.6); 本批表此时无人读, 零行为变化。
-- worker 直接挂 owner 列不用关联表: 一台 worker 必须属于且只属于一个用户(1:N),
-- 关联表是 M:N 语义(暗示可共享), 违背"worker 完全隔离"; 真金通道归属一列定死。
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;  -- web 登录(bcrypt); 空=只能 api key

-- 一人多把 key(网页/脚本各一把, 泄露吊销单把); 只存哈希, 明文仅签发瞬间显示一次
CREATE TABLE IF NOT EXISTS api_keys (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    key_hash     TEXT NOT NULL UNIQUE,
    name         VARCHAR(64),                       -- 备注: 谁的哪把钥匙
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 配置覆盖层: 用户自己的行有→用, 没有→落 config 全局默认。不复制快照(没改过的键实时跟随
-- 全局), 删自己的行=回默认; key 外键=只能覆盖真实存在的全局键 + 有覆盖的全局键删不掉(约束执法)
CREATE TABLE IF NOT EXISTS user_config (
    user_id INTEGER NOT NULL REFERENCES users(id),
    key     VARCHAR(64) NOT NULL REFERENCES config(key),
    value   JSONB NOT NULL,
    PRIMARY KEY (user_id, key)                      -- 一人一键一行, UPSERT 覆盖, 免维护
);

ALTER TABLE mt5_hosts ADD COLUMN IF NOT EXISTS
    owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id);
