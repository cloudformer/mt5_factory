-- 020_jobs.sql — jobs 队列: 数据库即任务队列(铁律5/6, 还"批量回测进度在api内存"的欠账)
--
-- 投递: /backtest/run 拆成 每策略×品种 一行(PENDING)后即返回, api 重启批次不丢
-- 消费: services/jobs.consumer_loop 用 FOR UPDATE SKIP LOCKED 抢单 — 任意多副本并发安全,
--       按 payload->>'symbol' 排序抢, 同品种连续执行以复用已加载的 M1(消费侧内存缓存)
-- 租约: RUNNING 超时(消费者死了)自动扫回 PENDING 重试, 超过尝试次数标 FAILED
-- 自清理(铁律3): 新批次提交时删光旧批次所有行 — 表内永远只有最新一批(≤batch_limit 条), 免维护
CREATE TABLE IF NOT EXISTS jobs (
    id          BIGSERIAL    PRIMARY KEY,
    kind        VARCHAR(32)  NOT NULL,                     -- 任务类型(目前只有 backtest)
    payload     JSONB        NOT NULL,                     -- {strategy_id, name, symbol, from, to, costs}
    status      VARCHAR(16)  NOT NULL DEFAULT 'PENDING',   -- PENDING / RUNNING / DONE / FAILED
    worker      VARCHAR(64),                               -- 谁在跑(host:pid, 排查用)
    attempts    INTEGER      NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
-- 抢单路径专用: PENDING 按 (品种, id) 排序取第一条
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs (kind, (payload->>'symbol'), id) WHERE status = 'PENDING';
