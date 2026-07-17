-- 023_config_recon_tol.sql — 关2对账: 回测与实盘时间窗口差距(分钟)进 config(配置页可改)
-- 用途: 逐笔配对容差 ±N分钟 + 对比窗口边缘放宽同值。实测成交滞后仅 3~8 秒, 默认 2 已有几十倍余量(且<最小周期M5的一半, 不会误配邻bar交易)
--; runner 错过收盘晚一根bar补单(如 M15=15分钟)将配不上 → 如实暴露为执行差异。
INSERT INTO config (key, value) VALUES ('recon_pair_tol_minutes', '2')
ON CONFLICT (key) DO NOTHING;
