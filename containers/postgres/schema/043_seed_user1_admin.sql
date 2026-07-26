-- 043_seed_user1_admin.sql — 种子对齐角色模型(2026-07-26): id 1 = owner = 纯管理, 名叫 admin
-- 033 的种子写于身份重构前(id1='frank'); 现役库已手术改名(2026-07-25 直接写库, 资产归 frank=id2 —
-- 那部分是环境专属手术, 不进 schema)。本文件只管通用部分: 新装环境 id1 也应叫 admin。
-- 幂等: 现役库 id1 已是 admin → 不匹配 = 无害跳过; 新装库 033 插完 'frank' → 这里改名。
UPDATE users SET name = 'admin' WHERE id = 1 AND name = 'frank';
