-- 031_config_volume_default.sql — 默认下单手数进 config(2026-07-20)
-- 原默认只在 worker env(VOLUME=0.01), web 看不见 → 页面下拉只能写"默认"不知道是多少。
-- 现在: config 表唯一源, 页面显示「0.01(默认)」且配置页可改; runner 拉策略时顺带取用,
-- env VOLUME 退化为 api 不可达时的最后兜底。
INSERT INTO config (key, value) VALUES ('volume_default', '0.01')
ON CONFLICT (key) DO NOTHING;
