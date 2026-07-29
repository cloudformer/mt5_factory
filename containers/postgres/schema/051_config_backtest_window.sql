-- 051_config_backtest_window.sql — 批量回测默认时间窗(2026-07-29 与 Frank 定):
-- M1 挖到 20 年后(XAUUSD 686万根), 批量回测再"有多少吃多少"会慢出天际且新旧批窗口漂移。
-- 批量/筛选回测一律用本窗口(天, 从提交时刻往回数, 冻结进 job); 按 ID 点名可在页面
-- 手动选 1/5/10/20 年(默认 5 年); 对账重放不受影响(窗口=实盘跨度, 天然对齐)。
-- 对比三铁律: 同一批同窗口, backtests 表存 from/to 可追溯, 换窗口重跑=新批不与旧批混比。
INSERT INTO config (key, value) VALUES ('backtest_window_days', '180')
ON CONFLICT (key) DO NOTHING;
