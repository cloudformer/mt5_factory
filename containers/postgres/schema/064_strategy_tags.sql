-- 064_strategy_tags.sql — 策略筛选履历 tags 列表(2026-08-08 与 Frank 定, v0.7 批次1)
-- basis(025) 回归本职 = 生成批次标签/生因(一段人写的文本);
-- 筛选履历独立成 list, 每跑一批【追加】一个元素(只增不改):
--   [{"report": "oos_v2#6", "status": "pass", "created_time": "2026-08-08T09:10:10+00:00"}]
-- report = 真实报告名(报告号可 JOIN 回报告表取全量数据); skip 不追加(留池重跑, 缺数据不定论)。
-- 存量: 早期写进 basis 的 oos_v2#N 老标签不追改, 出池过滤两处都认。
-- 设计: docs/2.regime_dirction/v0.7_Tag_Profile_预测验证设计.md
ALTER TABLE strategies ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb;
