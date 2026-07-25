-- 035_config_quota_keys.sql — v5.3 license 控制项进配置(2026-07-24)
-- 命名规约: quota_ 前缀 = 配额类(与 volume_default 等偏好类一眼区分)。
-- 全局默认宽松到不影响单用户现状; 给某用户设额度 = user_config 写他的覆盖行;
-- 未来 license/plan = 一组 quota 键值模板, 套餐=批量 UPSERT 覆盖行, 表结构不用动。
-- 本步无人读这些键(计数与拦截在 v5.6 之后"先有尺子后装闸门");
-- 口径(立此为据): 额度键用户自己不可覆盖(白名单在 v5.6 权限一起落), 否则配额形同虚设。
INSERT INTO config (key, value) VALUES
    ('quota_backtests_per_day',  '100000'),   -- 每日回测次数上限
    ('quota_strategies_max',     '100000'),   -- 策略实例总数上限
    ('quota_workers_max',        '100'),      -- worker 数上限
    ('quota_ai_reports_per_day', '100000')    -- 每日 AI 成绩单/分析上限
ON CONFLICT (key) DO NOTHING;
