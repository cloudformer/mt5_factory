-- 007_symbols_broker.sql — 品种加"券商"标注列(信息用, 不进主键)
--
-- 只是给品种标注"来自哪个券商"(登记时自动取账户 server 名), 便于将来接多个券商时区分来源。
-- 不改 historical_bars 主键、不支持同名品种多券商并存(那是重量版, 需要时另做)。
-- 存量行 broker 留空 → 视为当前默认券商, 下次校验时自动补上。
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS broker VARCHAR(64);
