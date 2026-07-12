-- 008_backtests_symbol.sql — 回测结果记录"跑在哪个品种/券商数据"上 (v1.3 跨品种验证)
--
-- 病根: 跨品种验证让同一策略实例(strategy_id)在多个品种上各回测一次, 但 backtests 表
--   没有品种列, 品种只能从 strategies.symbol 推出(单一) → 跨品种结果无处安放、互相混淆。
-- 修复: backtests 增加 symbol/broker 列, 记录该行实际跑在哪个品种/券商数据上。
--   主品种结果: symbol = strategies.symbol; 跨品种验证结果: symbol = 其它品种。
--   排名/晋级仍只认主品种行(symbol = strategies.symbol); 其余行用于跨品种健壮性摘要与明细。
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS symbol VARCHAR(32);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS broker VARCHAR(64);

-- 存量行回填: 老结果都是在各自策略的主品种上跑的
UPDATE backtests b SET symbol = s.symbol
  FROM strategies s WHERE b.strategy_id = s.id AND b.symbol IS NULL;

-- 券商标注回填: 从 symbols 表带上(单券商轻量版时就是那一个)
UPDATE backtests b SET broker = sy.broker
  FROM symbols sy WHERE b.symbol = sy.symbol AND b.broker IS NULL;

-- 健壮性汇总走 (strategy_id, symbol) 取每品种最新一次
CREATE INDEX IF NOT EXISTS idx_backtests_strategy_symbol
  ON backtests (strategy_id, symbol, created_at DESC);
