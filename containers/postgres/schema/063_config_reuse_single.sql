-- 063_config_reuse_single.sql — 单ID回测复用有效期(2026-08-08 与 Frank 定稿, 两档全局配置归 admin)
-- 批量/筛选回测(含自动化筛选v1/oos_v2 与其点名诊断) = backtest_reuse_days(schema/062, 默认7天);
-- 回测页「按ID点名」= 本键(默认1天, 键缺失回落全局) — 人工在看的策略要更新鲜。
-- 点名表单显式给出本次有效期(预填本键值, 可临时改; 填0=本次实际跑, 影响面=点名策略×品种数)。
-- 语义 = "跑回测时 N 天内的行直接复用", 不是数据过期作废。两键都只在「配置→策略参数」改(admin)。
INSERT INTO config (key, value) VALUES ('backtest_reuse_days_single', '1')
ON CONFLICT (key) DO NOTHING;
