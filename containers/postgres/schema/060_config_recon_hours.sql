-- 060_config_recon_hours.sql — 自动对账频率(2026-08-06 与 Frank 定)
-- 语义 = "距上次满 N 小时就跑"(不挑时辰, 与 auto_sync_hours 同款; 逻辑不写死钟点),
-- 心跳主节点搭车逐个重算全部有实盘成交的策略; 0 = 关闭(只留页面手动「全部重算」)。
-- 页面控件: 对账统计页「回测 vs 实盘 · 对账统计频率」下拉 3/6/12/24/36/72, 仅 admin 可改。
INSERT INTO config (key, value) VALUES ('recon_hours', '24')
ON CONFLICT (key) DO NOTHING;

-- 上次自动对账时刻(计时用; 与 sync_last_trigger 同款一行 UPSERT, 不留流水)
INSERT INTO config (key, value) VALUES ('recon_last_run', 'null')
ON CONFLICT (key) DO NOTHING;
