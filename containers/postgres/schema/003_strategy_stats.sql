-- 003_strategy_stats.sql — 每策略 × 环境(DEMO/LIVE) 的实跑战绩快照
--
-- 目的: 回测 vs demo vs live 三方并排对比 (代码完全一致, 差异只能来自券商执行);
--       策略晋级 LIVE 后 demo 战绩保留在这里, 不随 worker 停止加载而消失。
-- 来源: worker 心跳的 per_strategy (近90天滚动窗口), api 每30s 按主机角色 upsert。
-- 边界: 只存聚合快照, 不存逐笔明细 (成交逐笔回写是 P2)。
CREATE TABLE IF NOT EXISTS strategy_stats (
    strategy_id INTEGER          NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    env         VARCHAR(8)       NOT NULL CHECK (env IN ('DEMO', 'LIVE')),
    trades      INTEGER          NOT NULL DEFAULT 0,
    wins        INTEGER          NOT NULL DEFAULT 0,
    profit      DOUBLE PRECISION NOT NULL DEFAULT 0,   -- 已实现盈亏(含手续费/swap), 账户币种
    updated_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, env)
);
