-- 03_results.sql - 回测结果

CREATE TABLE backtests (
    id          SERIAL PRIMARY KEY,
    strategy_id INTEGER     NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    from_time   TIMESTAMPTZ NOT NULL,
    to_time     TIMESTAMPTZ NOT NULL,
    metrics     JSONB       NOT NULL,               -- trades/win_rate/net_points/profit_factor/max_dd...
    trades      JSONB       NOT NULL DEFAULT '[]',  -- 逐笔明细, demo 对账用
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_backtests_strategy ON backtests (strategy_id, created_at DESC);

SELECT '03_results.sql done' AS status;
