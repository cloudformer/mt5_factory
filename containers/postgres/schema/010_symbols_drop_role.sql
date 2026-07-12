-- 010_symbols_drop_role.sql — 去掉品种的 trade/validate 角色标记(简化)
--
-- 品种"做验证还是交易"由将来怎么用决定, 不需要在主档多一个字段标记(它此前也没被任何业务逻辑读)。
-- 005 加的 role 列在此移除。(005 每次启动会 IF NOT EXISTS 重新加, 本文件随后再删 → 幂等, 净结果无此列。)
ALTER TABLE symbols DROP COLUMN IF EXISTS role;
