-- 055_config_auto_sync.sql — Windows worker 自动下载同步间隔(2026-08-01 与 Frank 定)
-- 心跳主节点搭车: 每 N 小时自动投一批增量下载(M1+D1 按配置层, 断点续传幂等) —
-- 把"每天人肉点同步"自动化, regime 当日格的原料保鲜靠它(数据不新=带门策略保守不开仓)。
-- 配置页只读展示; 改值 = owner 直接库操作(UPDATE config ...); 0 = 关闭自动同步。
INSERT INTO config (key, value) VALUES ('auto_sync_hours', '6')
ON CONFLICT (key) DO NOTHING;
