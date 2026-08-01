-- 054_strategy_metadata.sql — 策略 metadata(v0.3 定稿, 2026-08-01 与 Frank 定)
-- params 管"信号怎么算", metadata 管"执行怎么裁"(regime 门→手数倍率; trail 将来迁入)。
-- {} = 无门全量交易(唯一写法); 有门 = {"regime":{"version":钉死的版本id,
--   "cells":{"ABA":1,"BBA":0.5}}} — 未列格不开新仓, 倍率 0.5~1。
-- 门 = 新实例(克隆带 metadata, parent_id 谱系): 唯一约束扩维后
-- 同参数+不同门 = 合法新实例; 同参数+同门 = 照旧判重。
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';

DO $$
BEGIN
    -- 旧约束(template,symbol,timeframe,params) → 扩成含 metadata; 幂等: 已换过则跳过
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'strategies_template_symbol_timeframe_params_key') THEN
        ALTER TABLE strategies
            DROP CONSTRAINT strategies_template_symbol_timeframe_params_key;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'strategies_identity_key') THEN
        ALTER TABLE strategies ADD CONSTRAINT strategies_identity_key
            UNIQUE (template, symbol, timeframe, params, metadata);
    END IF;
END $$;
