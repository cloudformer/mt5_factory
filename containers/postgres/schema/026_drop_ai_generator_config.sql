-- 026_drop_ai_generator_config.sql — 移除旧 AI 参数生成器协议(2026-07-17 拆除, 从未启用)
-- 外部 /propose 生成器已被 v2.2 AI 调参页(人桥粘贴 → ai_candidates 收货管道)取代;
-- 代码已删 generate 的 mode=ai / _ai_combos / 配置页入口, 此处清掉遗留配置键。
-- (001_base.sql 按规矩不改, 新库会先种入再被本文件删除, 结果一致)
DELETE FROM config WHERE key = 'ai_generator_url';
