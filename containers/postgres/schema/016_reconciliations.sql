-- 016_reconciliations.sql — 关2对账结果(派生, 见 v1.6_关2对账.md §3)
-- 一行 = 一个 (strategy_id, scope); scope='all' 现(demo+live合并), 未来加 'demo'/'live'。
-- 覆盖式(latest verdict): 重算即 upsert 覆盖, 不存历史(历史趋势未来单独建表, 不动本表)。
CREATE TABLE IF NOT EXISTS reconciliations (
    strategy_id   INTEGER          NOT NULL,
    scope         VARCHAR(8)       NOT NULL DEFAULT 'all',   -- all | demo | live
    window_from   TIMESTAMPTZ,                              -- 自动 = 实际成交时间跨度
    window_to     TIMESTAMPTZ,
    actual_trades INTEGER          NOT NULL DEFAULT 0,       -- 窗口内实盘/demo回合数
    bt_trades     INTEGER          NOT NULL DEFAULT 0,       -- 窗口内回测信号数
    match_score   DOUBLE PRECISION,                         -- 0~100 综合匹配度(门禁/过滤)
    metrics       JSONB            NOT NULL DEFAULT '{}',    -- 各一致率明细; 加维度不改表
    updated_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, scope)
);
