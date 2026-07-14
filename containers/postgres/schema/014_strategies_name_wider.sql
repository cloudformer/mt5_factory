-- 014_strategies_name_wider.sql — 策略名列加宽 128→256
--
-- 病根: 多参数模板(intraday_multi, 10个参数)拼出的实例名 >128 字符,
--       生成时被 VARCHAR(128) 拒(StringDataRightTruncation)。
-- 名字是"模板-品种-周期-全参数"的可读身份, 截断会破坏唯一性 → 加宽而非截断。
ALTER TABLE strategies ALTER COLUMN name TYPE VARCHAR(256);
