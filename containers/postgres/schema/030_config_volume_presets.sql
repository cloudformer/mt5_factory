-- 030_config_volume_presets.sql — 手数预设进 config(2026-07-18)
-- 策略列表改手数用下拉选预设值, 不再自由输入 — 防胖手指(LIVE 上 0.1 敲成 1 = 10倍仓位)。
-- 预设列表可在「配置·策略参数」页改; 空选项 = 清除(回 worker env 默认)。
INSERT INTO config (key, value) VALUES ('volume_presets', '[0.01, 0.02, 0.05, 0.1, 0.5, 1]')
ON CONFLICT (key) DO NOTHING;
