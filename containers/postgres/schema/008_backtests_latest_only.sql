-- 008_backtests_latest_only.sql — [已废弃, 由 009 取代]
--
-- 原设计: backtests"每策略只留最新一行"(DELETE 去重 + UNIQUE(strategy_id))。
-- ⚠ 已被 009 取代(每"策略×品种"一行, 支持跨品种验证)。原来的 DELETE 是销毁性的:
--    schema 每次启动按序全跑, 008 会把 009 写入的跨品种多行删回一行 → 每次 make up 都毁数据。
-- 故此处**移除** DELETE 与 UNIQUE(strategy_id); 键/约束的建立与切换全部交给 009。
-- 只保留一句幂等的"清掉旧唯一约束"(009 也会做, 这里做无害), 便于从旧库平滑过渡。
ALTER TABLE backtests DROP CONSTRAINT IF EXISTS backtests_strategy_uniq;
