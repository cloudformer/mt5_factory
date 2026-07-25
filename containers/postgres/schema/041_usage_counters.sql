-- 041_usage_counters.sql — 用量计数(2026-07-25 与 Frank 定): 只记录不拦截。
-- 单行设计(铁律3 不追加流水): 每 user × 指标 一行, 表大小恒定(用户数×3), 永不清理。
-- used_total 永远累加; day/day_used 记"最近活动日及其用量" — 跨天翻篇发生在新一天的
-- 第一次写入(CASE 覆盖), 无定时任务; day≠今天 即今日=0。
-- 记账归属 = 资源 owner(回测/AI报告记给策略主人, 建策略记 owner 列) — 无需登录即可归账。
-- 存量类(策略数/worker数)不进本表: COUNT 现查即真相, 复制一份只会漂移。
CREATE TABLE IF NOT EXISTS usage_counters (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    metric     VARCHAR(32) NOT NULL,          -- backtests / ai_reports / strategies_created
    used_total BIGINT  NOT NULL DEFAULT 0,
    day        DATE,
    day_used   INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, metric)
);
