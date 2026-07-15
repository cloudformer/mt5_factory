-- 015_trades.sql — 实盘/demo 逐笔成交回合(关2对账源数据, 见 v1.6_关2对账.md)
-- 粒度: 回合(round-trip), 一次开+平 = 一行(MT5 deal 腿在 API 侧按 position_id 配对)。
-- 只存已平仓回合; 持仓中仍实时拉 MT5, 不入库(遵 v1.2 §2 边界)。
-- 归因: magic → strategy_id (magic=100000+id); 每笔如实记 env(DEMO/LIVE)。
-- 维护: 心跳增量 upsert, 主键 (account, position_id) 去重, 只增不改, 永久保留。
CREATE TABLE IF NOT EXISTS trades (
    account      BIGINT           NOT NULL,           -- MT5 登录号(多账户隔离)
    position_id  BIGINT           NOT NULL,           -- 券商唯一仓位ID
    strategy_id  INTEGER,                             -- magic-100000; 手动/测试=NULL(当前只落策略回合)
    magic        BIGINT           NOT NULL,
    env          VARCHAR(8)       NOT NULL,           -- DEMO | LIVE (事实; 对账暂合并, 未来按此拆)
    symbol       VARCHAR(32)      NOT NULL,
    direction    VARCHAR(4)       NOT NULL,           -- buy | sell (持仓方向 = 开仓腿类型)
    volume       DOUBLE PRECISION NOT NULL,
    entry_time   TIMESTAMPTZ      NOT NULL,           -- 券商服务器时间
    entry_price  DOUBLE PRECISION NOT NULL,
    exit_time    TIMESTAMPTZ      NOT NULL,
    exit_price   DOUBLE PRECISION NOT NULL,
    sl           DOUBLE PRECISION,                    -- 预留(deal 腿不带, 暂 NULL)
    tp           DOUBLE PRECISION,
    close_reason VARCHAR(12),                         -- sl | tp | manual | expert
    profit       DOUBLE PRECISION NOT NULL,           -- 券商真实已实现盈亏(平仓腿)
    commission   DOUBLE PRECISION NOT NULL DEFAULT 0, -- 两腿手续费合计
    swap         DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_points   DOUBLE PRECISION,                    -- 毛价格位移换算点(symbols.point); 成本调整口径在对账层处理
    created_at   TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (account, position_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy_id, env);
CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades (entry_time);
