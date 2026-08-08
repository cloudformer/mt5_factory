-- 067_oos_v2_max_limit.sql — oos_v2 单次上限的硬顶进库(2026-08-08 与 Frank 定)
-- 原为代码常量 10000(不明参数不许住在代码里)。UI 不提供编辑口(同一行 config 不好分权限),
-- 要改直接改库: UPDATE config SET value = jsonb_set(value, '{max_limit}', '20000')
--               WHERE key = 'oos_v2';
-- 老库: || 合并新键(已有值不覆盖); 新库: 061 种子后本文件补键。幂等。
UPDATE config SET value = '{"max_limit": 10000}'::jsonb || value WHERE key = 'oos_v2';
