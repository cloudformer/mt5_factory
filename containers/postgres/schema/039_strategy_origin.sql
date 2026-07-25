-- 039_strategy_origin.sql — v5.4 市场溯源字段(2026-07-25)
-- origin_id = 从哪个公开策略复制(fork)来的。与 parent_id 不同: parent 是 AI 调参谱系,
-- origin 是市场复制关系。本步只加列(fork 动作随 5.6 隔离后上), 无人写无人读, 零行为变化。
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS origin_id INTEGER REFERENCES strategies(id);
