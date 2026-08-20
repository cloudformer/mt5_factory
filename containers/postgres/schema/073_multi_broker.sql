-- 073: v2.3 多券商·实例户口制(2026-08-19, 方案=docs/1.backtest/v2.3_多券商户口制施工方案.md)
-- 口径: broker=全名字符串(与既有 symbols.broker 同串), 库内永无拼接串;
--       单券商期间行为逐字节不变(broker 处处恒等默认值, 主键 broker 缀末位不动现有查询计划)。
-- ⚠ 首跑含 historical_bars 主键重建(大表) — 服务器上停写窗口内执行(手册见施工方案文档)。

-- 1) 大表: 加列秒级(PG11+ 元数据默认值, 不重写 6600 万行); 主键 3列 → 4列(broker 末位)
ALTER TABLE historical_bars ADD COLUMN IF NOT EXISTS broker varchar(64)
  NOT NULL DEFAULT 'MetaQuotes-Demo';
DO $$ BEGIN
  IF (SELECT count(*) FROM information_schema.key_column_usage
       WHERE table_name = 'historical_bars'
         AND constraint_name = 'historical_bars_pkey') = 3 THEN
    ALTER TABLE historical_bars DROP CONSTRAINT historical_bars_pkey;
    ALTER TABLE historical_bars ADD CONSTRAINT historical_bars_pkey
      PRIMARY KEY (symbol, timeframe, "time", broker);
  END IF;
END $$;

-- 2) symbols: broker 升为主键一半(各券商同名品种各一行, 精度各归各, 还清"挤一行共用point"欠账)
--    + mt5_name(券商端真名, runner 下单映射; NULL=与 symbol 同名)
UPDATE symbols SET broker = 'MetaQuotes-Demo' WHERE broker IS NULL;
ALTER TABLE symbols ALTER COLUMN broker SET NOT NULL;
ALTER TABLE symbols ALTER COLUMN broker SET DEFAULT 'MetaQuotes-Demo';
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS mt5_name varchar(64);

-- 3) regime_timeline: 时间线按 (symbol, broker) 分世界(每券商各自的天气史); FK 重挂复合键
ALTER TABLE regime_timeline ADD COLUMN IF NOT EXISTS broker varchar(64)
  NOT NULL DEFAULT 'MetaQuotes-Demo';
DO $$ BEGIN
  IF (SELECT count(*) FROM information_schema.key_column_usage
       WHERE table_name = 'symbols' AND constraint_name = 'symbols_pkey') = 1 THEN
    ALTER TABLE regime_timeline DROP CONSTRAINT IF EXISTS regime_timeline_symbol_fkey;
    ALTER TABLE symbols DROP CONSTRAINT symbols_pkey;
    ALTER TABLE symbols ADD CONSTRAINT symbols_pkey PRIMARY KEY (symbol, broker);
  END IF;
  IF (SELECT count(*) FROM information_schema.key_column_usage
       WHERE table_name = 'regime_timeline'
         AND constraint_name = 'regime_timeline_pkey') = 3 THEN
    ALTER TABLE regime_timeline DROP CONSTRAINT regime_timeline_pkey;
    ALTER TABLE regime_timeline ADD CONSTRAINT regime_timeline_pkey
      PRIMARY KEY (version_id, symbol, date, broker);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'regime_timeline_symbol_broker_fkey') THEN
    ALTER TABLE regime_timeline ADD CONSTRAINT regime_timeline_symbol_broker_fkey
      FOREIGN KEY (symbol, broker) REFERENCES symbols (symbol, broker) ON DELETE CASCADE;
  END IF;
END $$;

-- 4) strategies: 实例户口(生成时落户默认券商; 升 live 到别家 = 迁户口, 见方案 6b)
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS broker varchar(64)
  NOT NULL DEFAULT 'MetaQuotes-Demo';

-- 5) backtests: broker 补齐 + 唯一约束扩维(户口行 与 跨券商验证行 并存)
UPDATE backtests SET broker = 'MetaQuotes-Demo' WHERE broker IS NULL;
ALTER TABLE backtests ALTER COLUMN broker SET NOT NULL;
ALTER TABLE backtests ALTER COLUMN broker SET DEFAULT 'MetaQuotes-Demo';
DO $$ BEGIN
  -- 状态收敛式(新旧分开判断): 无论此前哪个文件把旧键复活, 每次启动都收敛到"只有三列新键"
  IF EXISTS (SELECT 1 FROM pg_constraint
              WHERE conname = 'backtests_strategy_symbol_uniq') THEN
    ALTER TABLE backtests DROP CONSTRAINT backtests_strategy_symbol_uniq;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'backtests_strategy_symbol_broker_uniq') THEN
    ALTER TABLE backtests ADD CONSTRAINT backtests_strategy_symbol_broker_uniq
      UNIQUE (strategy_id, symbol, broker);
  END IF;
END $$;

-- 6) 默认券商(研发尺 = MetaQuotes-Demo, 唯一 20 年全量; 配置页可见)
INSERT INTO config (key, value) VALUES ('default_broker', '"MetaQuotes-Demo"'::jsonb)
  ON CONFLICT (key) DO NOTHING;
