-- 044_host_events_eid_dedup.sql — worker 异常事件幂等入库(2026-07-26 与 Frank 定)
-- runner 只上报"状态变化"(QUOTE_LOST/QUOTE_OK/ORDER_FAIL/LOAD_FAIL, 每条带唯一 eid),
-- 全量决策日志留 worker 本地滚动文件 — 库只存值得看的, 月均几十条, 不长肉。
-- 心跳轮询会反复看到同一批事件(worker 缓冲最近50条) → 唯一索引 + ON CONFLICT DO NOTHING
-- 约束执法去重: api 不记"上次收到哪条"(服务无状态铁律)。
CREATE UNIQUE INDEX IF NOT EXISTS uq_host_events_eid
    ON mt5_host_events (host_id, (detail->>'eid'))
    WHERE (detail->>'eid') IS NOT NULL;
