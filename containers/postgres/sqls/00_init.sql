-- 00_init.sql - 基础配置
-- PostgreSQL 容器首次启动时按文件名顺序自动执行

SET timezone = 'UTC';

-- 通用: updated_at 自动更新触发器函数
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

SELECT '00_init.sql done' AS status;
