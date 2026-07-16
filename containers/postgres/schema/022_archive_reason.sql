-- 022_archive_reason.sql — 淘汰死因码: ARCHIVED 的策略记"为什么杀"(枚举码, 非自由文本)
-- 用途: AI 调参的负样本("这类参数死于留出段崩"), 避免再生成同类; 页面按码翻译成中文。
-- 码表(api 侧校验): manual(手动) / holdout_loss(留出段亏) / min_trades(笔数不足)
--                  / low_pf(盈亏比差) / recon_fail(对账不达标) / other
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS archive_reason VARCHAR(32);
