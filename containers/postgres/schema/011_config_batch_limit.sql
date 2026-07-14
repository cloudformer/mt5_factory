-- 011_config_batch_limit.sql — 单批回测上限进 config(可在配置页改, 不写死)
--
-- 原来 500 写死在代码默认里; 策略总数超过它时"全部"跑不全(每次同样排序取前500, 尾部永远轮不上)。
-- 现在: 配置页可改; 代码兜底仍 500(config 缺失时)。
INSERT INTO config (key, value) VALUES ('backtest_batch_limit', '500')
ON CONFLICT (key) DO NOTHING;
