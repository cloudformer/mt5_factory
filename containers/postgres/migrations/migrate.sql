-- 幂等迁移: api 每次启动自动执行, 把已有数据库对齐到当前代码要求的结构。
-- sqls/*.sql 只在空库首次初始化时跑; 本文件负责"已有数据的库"的增量对齐, 每次启动都跑一遍。
--
-- 规则: 只写增量、且必须幂等 (ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS / ON CONFLICT)。
--       用 ALTER TABLE IF EXISTS, 表还没建也安全跳过。以后每加一列/一表, 在这里补一行即可。

-- mt5_hosts: 历次新增的列
ALTER TABLE IF EXISTS mt5_hosts ADD COLUMN IF NOT EXISTS download BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE IF EXISTS mt5_hosts ADD COLUMN IF NOT EXISTS runner VARCHAR(8);
ALTER TABLE IF EXISTS mt5_hosts ADD COLUMN IF NOT EXISTS status VARCHAR(8) NOT NULL DEFAULT 'OFFLINE';
ALTER TABLE IF EXISTS mt5_hosts ADD COLUMN IF NOT EXISTS online_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS mt5_hosts ADD COLUMN IF NOT EXISTS offline_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS mt5_hosts ADD COLUMN IF NOT EXISTS last_health JSONB;

-- mt5_host_events: 后加的表
CREATE TABLE IF NOT EXISTS mt5_host_events (
    id         SERIAL PRIMARY KEY,
    host_id    INTEGER     NOT NULL REFERENCES mt5_hosts(id) ON DELETE CASCADE,
    event      VARCHAR(16) NOT NULL,
    detail     JSONB       NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_host_events ON mt5_host_events (host_id, created_at DESC);

-- mq5_imports: 一致性验证相关的后加列
ALTER TABLE IF EXISTS mq5_imports ADD COLUMN IF NOT EXISTS consistency DOUBLE PRECISION;
ALTER TABLE IF EXISTS mq5_imports ADD COLUMN IF NOT EXISTS verify_detail JSONB;
ALTER TABLE IF EXISTS mq5_imports ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

-- config: 后加的配置项种子 (已存在则不覆盖)
INSERT INTO config (key, value) VALUES
    ('ai_generator_url', '""'),
    ('backtest_costs', '{"slippage_points":3,"commission_points":7,"spread_points":null}')
ON CONFLICT (key) DO NOTHING;
