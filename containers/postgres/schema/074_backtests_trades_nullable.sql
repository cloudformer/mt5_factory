-- 074 归档瘦身(2026-08-20 Frank 定): 尸体(ARCHIVED 策略)的回测逐笔 trades 置 NULL,
-- metrics 留着喂排名「含归档」视图/负样本教材 — trades 列放开 NOT NULL。
-- 逐笔=可再生读数(原料 M1 在、尺子在, 复活重跑一次即回); 参数/basis/死因=资产在 strategies。
-- 复用守卫(backtest.reuse_ok/reuse_row)认 trades IS NOT NULL, 空壳行不会被误复用。幂等。
ALTER TABLE backtests ALTER COLUMN trades DROP NOT NULL;
