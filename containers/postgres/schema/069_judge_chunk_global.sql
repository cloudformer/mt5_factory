-- 069_judge_chunk_global.sql — 判定块大小升级为全局机器参数(2026-08-08 与 Frank 定:
-- 所有筛选器的收尾判定统一下放 worker)。它描述的是 worker 尺寸(2G 配 300), 不是某个
-- 模块的业务判据 — 全系统一个键, v1(regime_screen)/oos_v2 收尾共用。
-- 迁移: 沿用 oos_v2 里已调好的值(没有则 300), 然后从 oos_v2 键里摘除(配置只在一处)。
INSERT INTO config (key, value)
SELECT 'judge_chunk',
       COALESCE((SELECT value->'judge_chunk' FROM config WHERE key = 'oos_v2'), '300'::jsonb)
ON CONFLICT (key) DO NOTHING;
UPDATE config SET value = value - 'judge_chunk' WHERE key = 'oos_v2';
