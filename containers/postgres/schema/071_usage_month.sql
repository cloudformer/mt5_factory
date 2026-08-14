-- 071_usage_month.sql — 用量加「当月」维度(2026-08-14 与 Frank 定)
-- 页面维度定版: 今日回测|当月回测|今日AI|当月AI|活动策略|归档策略|worker数|有效期。
-- 当月与今日同一套单行翻篇设计(铁律3 不追加流水): month 记"最近活动月",
-- 跨月翻篇发生在新月份第一次写入(CASE 覆盖), 无定时任务; month≠本月 即当月=0。
-- 存量数据无法回填月份(单行只有累计), 当月计数从本文件上线起算 — 观察指标, 可接受。
ALTER TABLE usage_counters ADD COLUMN IF NOT EXISTS month DATE;
ALTER TABLE usage_counters ADD COLUMN IF NOT EXISTS month_used BIGINT NOT NULL DEFAULT 0;
