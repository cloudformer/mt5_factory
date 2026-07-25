-- 038_reconciliations_by_account.sql — v5.0-B2b: 对账结果换轴到账户(2026-07-25)
-- 对账单位从"策略"变"策略×账户"(多挂载后同策略多账户混在一起配对会互相冤枉)。
-- 旧行保留 account=0(旧口径合并结果), 下次重算时被整组删旧插新自然清掉。
ALTER TABLE reconciliations ADD COLUMN IF NOT EXISTS account BIGINT NOT NULL DEFAULT 0;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.key_column_usage
        WHERE table_name = 'reconciliations' AND constraint_name = 'reconciliations_pkey'
          AND column_name = 'account') THEN
        ALTER TABLE reconciliations DROP CONSTRAINT IF EXISTS reconciliations_pkey;
        ALTER TABLE reconciliations ADD PRIMARY KEY (strategy_id, scope, account);
    END IF;
END $$;
