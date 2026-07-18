-- 029_strategy_volume.sql — 每策略下单手数(仓位管理最小版, 2026-07-18)
-- NULL = 未设置, runner 用自己的 env VOLUME 默认(0.01) — 老策略行为不变。
-- 设了值: runner 每轮从 DB 拉实例配置, 下一单即用新手数(无状态, 实时生效不用重启);
-- 回测照旧只算净点(与手数无关); 折算金额 = 净点 × volume, 与实盘同一系数。
-- 逐笔对账金额层用 trades.volume(成交时快照), 中途改仓位不失真。
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS volume DOUBLE PRECISION;
