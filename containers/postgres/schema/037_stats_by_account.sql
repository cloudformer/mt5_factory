-- 037_stats_by_account.sql — v5.0-B1: strategy_stats 换轴到账户(2026-07-24)
-- 多挂载后同策略可在多账户同时跑, 旧主键 (strategy_id, env) 会互相覆盖 →
-- 主键改 (strategy_id, account), env 降为属性列(所有按 env 聚合的读法保留, SUM 过账户)。
-- 红利: 对账升级为"回测 vs 每个账户并排"的数据基础(对账按账户切分在 B2)。
ALTER TABLE strategy_stats ADD COLUMN IF NOT EXISTS account BIGINT NOT NULL DEFAULT 0;

-- 老行回填: 该 env 角色当前启用主机的账户(单机期映射唯一且 002 保证 demo/live 账户不同);
-- 映射不到的用 -1(DEMO)/-2(LIVE) 哨兵保行不撞主键(历史快照保留, "晋级后旧环境战绩不丢")
UPDATE strategy_stats s SET account = COALESCE(
    (SELECT h.mt5_login FROM mt5_hosts h
      WHERE h.runner = lower(s.env) AND h.enabled AND h.mt5_login IS NOT NULL
      ORDER BY h.id LIMIT 1),
    CASE s.env WHEN 'DEMO' THEN -1 ELSE -2 END)
 WHERE s.account = 0;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.key_column_usage
        WHERE table_name = 'strategy_stats' AND constraint_name = 'strategy_stats_pkey'
          AND column_name = 'account') THEN
        ALTER TABLE strategy_stats DROP CONSTRAINT IF EXISTS strategy_stats_pkey;
        ALTER TABLE strategy_stats ADD PRIMARY KEY (strategy_id, account);
    END IF;
END $$;
