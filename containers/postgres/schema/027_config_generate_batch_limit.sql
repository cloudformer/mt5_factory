-- 027_config_generate_batch_limit.sql — 生成单批收货上限进 config(可在生成页改, 不写死)
--
-- 原来 500 写死在收货管道(services/instances)常量里; 与 backtest_batch_limit 同规矩:
-- config 可改, 代码兜底仍 500(config 缺失时)。所有生成入口(批量生成/AI调参)共用此上限。
INSERT INTO config (key, value) VALUES ('generate_batch_limit', '500')
ON CONFLICT (key) DO NOTHING;
