-- 049_config_download_timeframes.sql — 下载周期层进配置(2026-07-29 与 Frank 定, 不藏代码里)
-- M1 = 唯一原始数据(回测/对账/聚合的原料), 永远必含;
-- D1 = 例外补下(CLAUDE.md 预留例外正式启用): MetaQuotes-Demo 实测 M1 仅存~4个月,
--      而 D1 有 16 年+ — regime/长视野用原生 D1 补头, 回测仍只读 M1(尺子不换料)。
-- 中间层(H1/M15 等)默认不下: 无消费者不囤数据; 将来要 = 配置页勾选即可, 管道已参数化。
INSERT INTO config (key, value) VALUES ('download_timeframes', '["M1", "D1"]')
ON CONFLICT (key) DO NOTHING;
