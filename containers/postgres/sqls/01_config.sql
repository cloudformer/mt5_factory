-- 01_config.sql - 配置层: MT5 worker 注册表 + 策略表

-- ========== MT5 Worker 注册表 ==========
-- 每台 Windows VM 上的 MT5 bridge 注册一行; 加 worker = 插一行, 不改代码
-- 职能两字段 (约束靠结构):
--   download: 是否承担数据下载 (可多台并行)
--   runner:   跑什么策略 demo|live|NULL(不跑) — 单字段天然保证 demo/live 互斥
CREATE TABLE mt5_hosts (
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
    status         VARCHAR(8)   NOT NULL DEFAULT 'OFFLINE'
                   CHECK (status IN ('ONLINE', 'OFFLINE')),
    online_at      TIMESTAMPTZ,             -- 最近一次上线时间
    offline_at     TIMESTAMPTZ,             -- 最近一次下线时间
    last_heartbeat TIMESTAMPTZ,
    last_health    JSONB,                   -- 最近一次 /health 完整响应 (web 展示用)
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),  -- 注册时间
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (host, port)
);

CREATE INDEX idx_mt5_hosts_enabled ON mt5_hosts (enabled);

CREATE TRIGGER trg_mt5_hosts_updated_at
    BEFORE UPDATE ON mt5_hosts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ========== Worker 事件历史 (完整追踪主机生命周期) ==========
-- REGISTERED / ONLINE / OFFLINE / ENABLED / DISABLED / ROLES_CHANGED / ACCOUNT_SET
CREATE TABLE mt5_host_events (
    id         SERIAL PRIMARY KEY,
    host_id    INTEGER     NOT NULL REFERENCES mt5_hosts(id) ON DELETE CASCADE,
    event      VARCHAR(16) NOT NULL,
    detail     JSONB       NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_host_events ON mt5_host_events (host_id, created_at DESC);

-- ========== 策略表（策略实例） ==========
-- 一行 = 模板 + 参数 + 品种 + 周期 的一个实例, 独立走准入漏斗
-- 状态机: CANDIDATE(生成) → DEMO(模拟盘验证) → LIVE(真钱实盘) / ARCHIVED(淘汰); 任意状态可互转
CREATE TABLE strategies (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(128) NOT NULL UNIQUE,
    template     VARCHAR(64)  NOT NULL,           -- strategy_core 中的模板名
    symbol       VARCHAR(32)  NOT NULL,
    timeframe    VARCHAR(8)   NOT NULL,           -- 决策周期 (M5/M15/H1/...)
    params       JSONB        NOT NULL DEFAULT '{}',
    magic_number INTEGER      UNIQUE,             -- 实盘/demo 订单归因, 进入 DEMO 时分配
    status       VARCHAR(16)  NOT NULL DEFAULT 'CANDIDATE'
                 CHECK (status IN ('CANDIDATE', 'DEMO', 'LIVE', 'ARCHIVED')),
    description  TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (template, symbol, timeframe, params)  -- 防重复生成同一实例
);

CREATE INDEX idx_strategies_status ON strategies (status);
CREATE INDEX idx_strategies_symbol ON strategies (symbol, status);

CREATE TRIGGER trg_strategies_updated_at
    BEFORE UPDATE ON strategies
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ========== 系统配置 (web/API 可查改, 不用改环境变量) ==========
CREATE TABLE config (
    key        VARCHAR(64) PRIMARY KEY,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_config_updated_at
    BEFORE UPDATE ON config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO config (key, value) VALUES
    ('symbols',    '["EURUSD","GBPUSD","USDJPY","XAUUSD","AUDUSD","USDCAD","NZDUSD","EURJPY","GBPJPY"]'),
    ('data_start', '"2015-01-01"'),
    ('ai_generator_url', '""'),   -- AI 参数生成器地址(可选), 如 http://host:9000
    ('backtest_costs', '{"slippage_points": 3, "commission_points": 7, "spread_points": null}');

-- ========== MQ5 转化流水线 ==========
-- 外部 MQ5 策略纳入系统的跟踪: 提交源码 → 评估 → 翻译成 strategy_core 模板
-- status: PENDING(待评估) | ASSESSED(已评估) | TRANSLATED(已翻译, template字段指向模板) | REJECTED(不纳入)
CREATE TABLE mq5_imports (
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

CREATE TRIGGER trg_mq5_imports_updated_at
    BEFORE UPDATE ON mq5_imports
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ========== 品种表 ==========
-- point 用于回测点数换算; 后续可由 bridge /symbol 数据校准
CREATE TABLE symbols (
    symbol VARCHAR(32) PRIMARY KEY,
    digits INTEGER          NOT NULL,
    point  DOUBLE PRECISION NOT NULL
);

INSERT INTO symbols (symbol, digits, point) VALUES
    ('EURUSD', 5, 0.00001), ('GBPUSD', 5, 0.00001), ('AUDUSD', 5, 0.00001),
    ('USDCAD', 5, 0.00001), ('NZDUSD', 5, 0.00001),
    ('USDJPY', 3, 0.001),   ('EURJPY', 3, 0.001),   ('GBPJPY', 3, 0.001),
    ('XAUUSD', 2, 0.01);

SELECT '01_config.sql done' AS status;
