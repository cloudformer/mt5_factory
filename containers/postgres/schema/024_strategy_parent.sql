-- 024_strategy_parent.sql — 策略谱系: AI 调参(v2.2)生成的实例记"从谁调出来的"
-- 用途: AI策略分析页的父子对比表 / finetune 迭代树 / 成绩单溯源。手工生成的实例为 NULL。
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS parent_id INTEGER REFERENCES strategies(id);
CREATE INDEX IF NOT EXISTS idx_strategies_parent ON strategies (parent_id) WHERE parent_id IS NOT NULL;
