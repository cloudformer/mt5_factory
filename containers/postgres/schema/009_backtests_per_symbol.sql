-- 009_backtests_per_symbol.sql — 跨品种验证(乙): backtests 每"策略×品种"一行
--
-- 008 是"一策略一行"(UNIQUE strategy_id)。引入跨品种健壮性后, 同一参数要在多个货币对各留一行:
--   symbol = 主品种 的那行 = 主结果(排名只认它); 其余行 = 跨品种验证结果(喂健壮性列/明细)。
-- 仍是 upsert(键 strategy_id+symbol), 重跑覆盖 → 行数有界(策略数 × 测过的品种数), 不无限增长。
-- 券商(broker)是每个品种自带的标签(轻量版下 品种↔券商 1:1), 只随行带着, 不进键。
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS symbol VARCHAR(32);
ALTER TABLE backtests ADD COLUMN IF NOT EXISTS broker VARCHAR(64);

-- 存量行(008 后每策略一行, 都是在各自主品种上跑的)回填 symbol/broker
UPDATE backtests b SET symbol = s.symbol
  FROM strategies s WHERE b.strategy_id = s.id AND b.symbol IS NULL;
UPDATE backtests b SET broker = sy.broker
  FROM symbols sy WHERE b.symbol = sy.symbol AND b.broker IS NULL;

-- 键: 去掉旧的 (strategy_id) 唯一, 换成 (strategy_id, symbol) 唯一(供 ON CONFLICT upsert)
DELETE FROM backtests WHERE symbol IS NULL;   -- 兜底: 无主策略的残行(极少见), 清掉才能进唯一键
ALTER TABLE backtests ALTER COLUMN symbol SET NOT NULL;
ALTER TABLE backtests DROP CONSTRAINT IF EXISTS backtests_strategy_uniq;
ALTER TABLE backtests DROP CONSTRAINT IF EXISTS backtests_strategy_symbol_uniq;
ALTER TABLE backtests ADD CONSTRAINT backtests_strategy_symbol_uniq UNIQUE (strategy_id, symbol);

CREATE INDEX IF NOT EXISTS idx_backtests_strategy_symbol
  ON backtests (strategy_id, symbol, created_at DESC);
