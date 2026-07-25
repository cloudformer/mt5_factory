-- 033_users_strategy_owner.sql — v5.1 多用户地基·第一步: users 最小表 + 策略归属打标(2026-07-24)
-- 只加不变: 现有数据全归 user 1(frank), 全站行为零变化(无人读新列);
-- 认证字段在 034 补, 认证/过滤等执法在 v5.6 才接; 唯一约束分域【待拍板】不在本文件。
CREATE TABLE IF NOT EXISTS users (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(64) NOT NULL UNIQUE,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,   -- false=一刀切停用(key/登录/worker 全失效, v5.6 执法)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO users (id, name) VALUES (1, 'frank') ON CONFLICT DO NOTHING;
-- 显式 id 插入不推进序列, 不对齐的话未来第一个新用户会撞 id=1
SELECT setval('users_id_seq', (SELECT max(id) FROM users));

ALTER TABLE strategies ADD COLUMN IF NOT EXISTS
    owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id);
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS
    visibility VARCHAR(8) NOT NULL DEFAULT 'private'
    CHECK (visibility IN ('private', 'public', 'shared'));
