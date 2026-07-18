-- 025_strategy_basis.sql — 策略生因: 实例创建时的依据(与死因 archive_reason 对偶)
-- 来源即收货管道协议里的 basis 字段: AI 调参附的一句依据 / 'grid' / 'random'。
-- 用途: 家族对比溯源"当初为什么生它"; 成绩单 failed_neighbors 给 AI 时生因死因齐全。
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS basis TEXT;
