-- 032_config_guard.sql — 关键配置键删除保护(2026-07-20, 约束执法)
-- volume_default 是 runner 下单的默认手数: 键一旦消失, api 响应缺字段, runner 退到 env 兜底,
-- 页面「X(默认)」也没了。值可以改(PUT 有校验), 但键必须永远存在 — 触发器在库侧执法,
-- 任何路径(未来代码 bug / 手滑 SQL)删它都直接报错。要加保护键: 往 IN 列表追加即可。
CREATE OR REPLACE FUNCTION config_guard_del() RETURNS trigger AS $$
BEGIN
    IF OLD.key IN ('volume_default') THEN
        RAISE EXCEPTION 'config key "%" is protected (交易关键配置, 只能改值不能删键)', OLD.key;
    END IF;
    RETURN OLD;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS config_guard_del ON config;
CREATE TRIGGER config_guard_del BEFORE DELETE ON config
    FOR EACH ROW EXECUTE FUNCTION config_guard_del();
