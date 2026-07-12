-- 008_backtests_latest_only.sql — backtests 每策略只留最新一次(免维护, 表不增长)
--
-- 原来每跑一次回测、每策略 INSERT 一行 → 历史累积, 表越来越大, trades 逐笔明细占空间。
-- 但结果排名/战绩本来就只取每策略"最新一行", 历史没在页面用到 → 改为"一策略一行, 重跑覆盖"。
-- (与 strategy_stats 同样的做法 → 永不增长, 不需要清理脚本。)
--
-- 1) 清理: 每个策略只保留最新(created_at 最大, 同刻取 id 最大)那一行, 其余删除。
--    幂等: 无重复时 DELETE 命中 0 行。
DELETE FROM backtests b
 USING backtests newer
 WHERE b.strategy_id = newer.strategy_id
   AND (newer.created_at, newer.id) > (b.created_at, b.id);

-- 2) 唯一约束: 之后回测写入用 ON CONFLICT (strategy_id) 覆盖式 upsert, 由 DB 保证一策略一行。
--    幂等: 先 DROP IF EXISTS 再 ADD。
ALTER TABLE backtests DROP CONSTRAINT IF EXISTS backtests_strategy_uniq;
ALTER TABLE backtests ADD CONSTRAINT backtests_strategy_uniq UNIQUE (strategy_id);
