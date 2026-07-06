-- 01_config.sql - 配置层: MT5 worker 注册表 + 策略表

-- ========== MT5 Worker 注册表 ==========
-- 每台 Windows VM 上的 MT5 bridge 注册一行; 加 worker = 插一行, 不改代码
-- roles: download(下载历史数据) | backtest(模拟回测) | live(实盘下单)
-- 测试期一台机器可同时持有多个角色, 拆分时改注册即可
CREATE TABLE mt5_hosts (
    id             SERIAL PRIMARY KEY,
    name           VARCHAR(64)  NOT NULL UNIQUE,
    host           VARCHAR(255) NOT NULL,
    port           INTEGER      NOT NULL DEFAULT 9090,
    roles          TEXT[]       NOT NULL DEFAULT '{}',
    mt5_login      BIGINT,
    mt5_server     VARCHAR(128),
    account_type   VARCHAR(8)   NOT NULL DEFAULT 'DEMO'
                   CHECK (account_type IN ('DEMO', 'REAL')),
    enabled        BOOLEAN      NOT NULL DEFAULT TRUE,
    last_heartbeat TIMESTAMPTZ,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (host, port)
);

CREATE INDEX idx_mt5_hosts_enabled ON mt5_hosts (enabled);
CREATE INDEX idx_mt5_hosts_roles ON mt5_hosts USING GIN (roles);

CREATE TRIGGER trg_mt5_hosts_updated_at
    BEFORE UPDATE ON mt5_hosts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ========== 策略表（策略实例） ==========
-- 一行 = 模板 + 参数 + 品种 + 周期 的一个实例, 独立走准入漏斗
-- 状态机: CANDIDATE(生成) → DEMO(回测通过,假钱实测) → ACTIVE(实盘) / ARCHIVED(任一关淘汰)
CREATE TABLE strategies (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(128) NOT NULL UNIQUE,
    template     VARCHAR(64)  NOT NULL,           -- strategy_core 中的模板名
    symbol       VARCHAR(32)  NOT NULL,
    timeframe    VARCHAR(8)   NOT NULL,           -- 决策周期 (M5/M15/H1/...)
    params       JSONB        NOT NULL DEFAULT '{}',
    magic_number INTEGER      UNIQUE,             -- 实盘/demo 订单归因, 进入 DEMO 时分配
    status       VARCHAR(16)  NOT NULL DEFAULT 'CANDIDATE'
                 CHECK (status IN ('CANDIDATE', 'DEMO', 'ACTIVE', 'ARCHIVED')),
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
    ('data_start', '"2015-01-01"');

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
