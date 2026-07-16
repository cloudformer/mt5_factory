-- 017_config_mt5_trades_days.sql — MT5 流水时间预设进 config(配置页可改)
-- 值 = 天数预设列表, 流水页(Worker/系统)渲染成快捷 chips(近N天); 另有"自定义"区间兜底。
INSERT INTO config (key, value) VALUES ('mt5_trades_days', '[7, 30, 90]')
ON CONFLICT (key) DO NOTHING;
