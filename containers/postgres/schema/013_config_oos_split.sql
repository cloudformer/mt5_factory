-- 013_config_oos_split.sql — OOS 样本外切分比例进 config(v1.3 #1, 配置页可调)
--
-- 值 = 训练段占比: 0.7 = 训练7:留出3(默认), 0.5 = 5:5, 0.3 = 3:7。
-- 纪律: 训练段用来选, 留出段只一票否决(留出亏 = 过拟合嫌疑, 不进 demo)。
INSERT INTO config (key, value) VALUES ('backtest_oos_split', '0.7')
ON CONFLICT (key) DO NOTHING;
