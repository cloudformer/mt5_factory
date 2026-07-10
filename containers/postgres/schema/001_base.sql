-- 001_base.sql — schema 唯一来源(第一卷): 截至 2026-07 的全部结构
--
-- 执行机制: api 每次启动按文件名顺序执行本目录全部 *.sql (见 api/src/main.py lifespan)。
-- 规则(必须遵守, 否则老库启动会炸):
--   1. 每条语句幂等: IF NOT EXISTS / CREATE OR REPLACE / DROP ... IF EXISTS / ON CONFLICT DO NOTHING
--   2. 已发布的文件永不修改 —— 任何变更 = 新增 002_xxx.sql (编号递增, 语义命名)
--   3. 空库跑一遍 = 建出全量; 老库跑一遍 = 无害对齐。没有单独的"迁移"概念

-- ========== 通用: updated_at 自动更新触发器函数 ==========
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ========== MT5 Worker 注册表 ==========
-- 每台 Windows VM 上的 MT5 bridge 注册一行; 加 worker = 插一行, 不改代码
-- 职能两字段 (约束靠结构): download 是否承担下载; runner demo|live|NULL — 单字段天然互斥
CREATE TABLE IF NOT EXISTS mt5_hosts (
    id             SERIAL PRIMARY KEY,
    name           VARCHAR(64)  NOT NULL UNIQUE,
    host           VARCHAR(255) NOT NULL,
    port           INTEGER      NOT NULL DEFAULT 8020,
    download       BOOLEAN      NOT NULL DEFAULT TRUE,
    runner         VARCHAR(8)   CHECK (runner IN ('demo', 'live')),  -- NULL=不跑策略
    mt5_login      BIGINT,
    mt5_server     VARCHAR(128),
    account_type   VARCHAR(8)   NOT NULL DEFAULT 'DEMO'
                   CHECK (account_type IN ('DEMO', 'REAL')),
    enabled        BOOLEAN      NOT NULL DEFAULT TRUE,
    status         VARCHAR(8)   NOT NULL DEFAULT 'OFFLINE',
    online_at      TIMESTAMPTZ,             -- 最近一次上线时间
    offline_at     TIMESTAMPTZ,             -- 最近一次下线时间
    last_heartbeat TIMESTAMPTZ,
    last_health    JSONB,                   -- 最近一次 /health 完整响应 (web 展示用)
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (host, port)
);
-- 老库对齐: 历次新增的列
ALTER TABLE mt5_hosts ADD COLUMN IF NOT EXISTS download BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE mt5_hosts ADD COLUMN IF NOT EXISTS runner VARCHAR(8);
ALTER TABLE mt5_hosts ADD COLUMN IF NOT EXISTS status VARCHAR(8) NOT NULL DEFAULT 'OFFLINE';
ALTER TABLE mt5_hosts ADD COLUMN IF NOT EXISTS online_at TIMESTAMPTZ;
ALTER TABLE mt5_hosts ADD COLUMN IF NOT EXISTS offline_at TIMESTAMPTZ;
ALTER TABLE mt5_hosts ADD COLUMN IF NOT EXISTS last_health JSONB;
-- status 三态 (DEGRADED = bridge 可达但 MT5 未就绪/账户未登录, 可远程下发账户)
ALTER TABLE mt5_hosts DROP CONSTRAINT IF EXISTS mt5_hosts_status_check;
ALTER TABLE mt5_hosts ADD CONSTRAINT mt5_hosts_status_check
    CHECK (status IN ('ONLINE', 'OFFLINE', 'DEGRADED'));

CREATE INDEX IF NOT EXISTS idx_mt5_hosts_enabled ON mt5_hosts (enabled);

CREATE OR REPLACE TRIGGER trg_mt5_hosts_updated_at
    BEFORE UPDATE ON mt5_hosts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ========== Worker 事件历史 ==========
-- REGISTERED / ONLINE / OFFLINE / DEGRADED / ENABLED / DISABLED / ROLES_CHANGED / ACCOUNT_SET / MAINTAIN
CREATE TABLE IF NOT EXISTS mt5_host_events (
    id         SERIAL PRIMARY KEY,
    host_id    INTEGER     NOT NULL REFERENCES mt5_hosts(id) ON DELETE CASCADE,
    event      VARCHAR(16) NOT NULL,
    detail     JSONB       NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_host_events ON mt5_host_events (host_id, created_at DESC);

-- ========== 策略表（策略实例） ==========
-- 一行 = 模板 + 参数 + 品种 + 周期 的一个实例, 独立走准入漏斗
-- 状态机: CANDIDATE(生成) → DEMO(模拟盘验证) → LIVE(真钱实盘) / ARCHIVED(淘汰); 任意状态可互转
CREATE TABLE IF NOT EXISTS strategies (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(128) NOT NULL UNIQUE,
    template     VARCHAR(64)  NOT NULL,           -- strategy_core 中的模板名
    symbol       VARCHAR(32)  NOT NULL,
    timeframe    VARCHAR(8)   NOT NULL,           -- 决策周期 (M5/M15/H1/...)
    params       JSONB        NOT NULL DEFAULT '{}',
    magic_number INTEGER      UNIQUE,             -- 实盘/demo 订单归因, 进入 DEMO 时分配
    status       VARCHAR(16)  NOT NULL DEFAULT 'CANDIDATE',
    description  TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (template, symbol, timeframe, params)  -- 防重复生成同一实例
);
-- 状态命名统一 ACTIVE→LIVE (先松约束再改数据)
ALTER TABLE strategies DROP CONSTRAINT IF EXISTS strategies_status_check;
UPDATE strategies SET status='LIVE' WHERE status='ACTIVE';
ALTER TABLE strategies ADD CONSTRAINT strategies_status_check
    CHECK (status IN ('CANDIDATE', 'DEMO', 'LIVE', 'ARCHIVED'));

CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies (status);
CREATE INDEX IF NOT EXISTS idx_strategies_symbol ON strategies (symbol, status);

CREATE OR REPLACE TRIGGER trg_strategies_updated_at
    BEFORE UPDATE ON strategies
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ========== 系统配置 (web/API 可查改, 不用改环境变量) ==========
CREATE TABLE IF NOT EXISTS config (
    key        VARCHAR(64) PRIMARY KEY,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE TRIGGER trg_config_updated_at
    BEFORE UPDATE ON config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 配置种子 (已存在则不覆盖)
INSERT INTO config (key, value) VALUES
    ('symbols',    '["EURUSD","GBPUSD","USDJPY","XAUUSD","AUDUSD","USDCAD","NZDUSD","EURJPY","GBPJPY"]'),
    ('data_start', '"2015-01-01"'),
    ('ai_generator_url', '""'),   -- AI 参数生成器地址(可选), 如 http://host:9000
    ('backtest_costs', '{"slippage_points": 3, "commission_points": 7, "spread_points": null}')
ON CONFLICT (key) DO NOTHING;

-- ========== MQ5 转化流水线 ==========
-- 外部 MQ5 策略纳入系统的跟踪: 提交源码 → 评估 → 翻译成 strategy_core 模板
CREATE TABLE IF NOT EXISTS mq5_imports (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(128) NOT NULL,
    source     TEXT         NOT NULL,           -- .mq5 源码
    params_set TEXT,                            -- .set 参数(可选)
    status     VARCHAR(16)  NOT NULL DEFAULT 'PENDING'
               CHECK (status IN ('PENDING', 'ASSESSED', 'TRANSLATED', 'REJECTED')),
    assessment TEXT,                            -- 评估结论(可直翻/需扩展/不收 + 原因)
    template   VARCHAR(64),                     -- 翻译后对应的 strategy_core 模板名
    consistency DOUBLE PRECISION,               -- 一致性验证结果(%)
    verify_detail JSONB,                        -- 比对明细(双方笔数/匹配数)
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);
-- 老库对齐: 一致性验证相关的后加列
ALTER TABLE mq5_imports ADD COLUMN IF NOT EXISTS consistency DOUBLE PRECISION;
ALTER TABLE mq5_imports ADD COLUMN IF NOT EXISTS verify_detail JSONB;
ALTER TABLE mq5_imports ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

CREATE OR REPLACE TRIGGER trg_mq5_imports_updated_at
    BEFORE UPDATE ON mq5_imports
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ========== 品种表 ==========
-- point 用于回测点数换算; 后续可由 bridge /symbol 数据校准
CREATE TABLE IF NOT EXISTS symbols (
    symbol VARCHAR(32) PRIMARY KEY,
    digits INTEGER          NOT NULL,
    point  DOUBLE PRECISION NOT NULL
);

INSERT INTO symbols (symbol, digits, point) VALUES
    ('EURUSD', 5, 0.00001), ('GBPUSD', 5, 0.00001), ('AUDUSD', 5, 0.00001),
    ('USDCAD', 5, 0.00001), ('NZDUSD', 5, 0.00001),
    ('USDJPY', 3, 0.001),   ('EURJPY', 3, 0.001),   ('GBPJPY', 3, 0.001),
    ('XAUUSD', 2, 0.01)
ON CONFLICT (symbol) DO NOTHING;

-- ========== 数据层: 历史K线 (按年分区) ==========
-- 字段对齐 MT5 copy_rates_* 返回结构; 只下载 M1, 高周期从 M1 聚合派生
CREATE TABLE IF NOT EXISTS historical_bars (
    symbol      VARCHAR(32)      NOT NULL,
    timeframe   VARCHAR(8)       NOT NULL,  -- M1/M5/M15/M30/H1/H4/D1/W1/MN1
    time        TIMESTAMPTZ      NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    tick_volume BIGINT           NOT NULL DEFAULT 0,
    spread      INTEGER          NOT NULL DEFAULT 0,
    real_volume BIGINT           NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, timeframe, time)
) PARTITION BY RANGE (time);

-- 42 个分区全覆盖, 任何时间写入都不报错:
--   pre2000 (最早→2000) + 2000~2039 共40个年度分区 + post2040 (2040→最晚)
CREATE TABLE IF NOT EXISTS historical_bars_pre2000 PARTITION OF historical_bars
    FOR VALUES FROM (MINVALUE) TO ('2000-01-01');

DO $$
DECLARE
    y INTEGER;
BEGIN
    FOR y IN 2000..2039 LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS historical_bars_%s PARTITION OF historical_bars
             FOR VALUES FROM (%L) TO (%L)',
            y, make_date(y, 1, 1), make_date(y + 1, 1, 1)
        );
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS historical_bars_post2040 PARTITION OF historical_bars
    FOR VALUES FROM ('2040-01-01') TO (MAXVALUE);

-- ========== 回测结果 ==========
CREATE TABLE IF NOT EXISTS backtests (
    id          SERIAL PRIMARY KEY,
    strategy_id INTEGER     NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    from_time   TIMESTAMPTZ NOT NULL,
    to_time     TIMESTAMPTZ NOT NULL,
    metrics     JSONB       NOT NULL,               -- trades/win_rate/net_points/profit_factor/max_dd...
    trades      JSONB       NOT NULL DEFAULT '[]',  -- 逐笔明细, demo 对账用
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_backtests_strategy ON backtests (strategy_id, created_at DESC);
