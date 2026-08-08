-- 066_jobs_result.sql — jobs 加 result 列(2026-08-08 与 Frank 定: 判定下放 worker)
-- 背景: oos_v2 全池收尾原在 api 单核判 6100 个(27GB/20分钟, 事件循环被霸占全线超时)。
-- 改为: 回测跑完 → 收尾切 500/块投「oos_v2_judge」判定任务 → worker 并行判(每块~1.5G,
-- 贴 worker 2G 预算), 逐块把明细写回本列 → api 只合并出报告(秒级) → 删队列自清理。
-- result 只在判定工种使用, 报告落库后随队列删除 — 不留需要清理的数据(铁律3)。
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result JSONB;
