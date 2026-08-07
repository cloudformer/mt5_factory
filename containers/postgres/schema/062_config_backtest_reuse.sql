-- 062_config_backtest_reuse.sql — 回测复用有效期(2026-08-07 与 Frank 定, 全局唯一配置)
-- 规则(唯一实现在 services/backtest.reuse_row, 所有回测路径共用):
--   该(策略×品种)的 backtests 行是 N 天内跑的 且 跨度覆盖本次要求窗口(差45天容差)
--   → 直接复用不重跑(大窗行永远能当小窗用: 20年行喂饱 v1 的5年切片与 v2 的20/5/2)。
-- 0 = 关闭(每次都现跑) — 需要精确同窗对比时用。
-- 覆盖: 批量回测/单ID点名(同走 jobs 队列)/自动化筛选v1/oos_v2/两筛选的点名诊断;
-- 不覆盖: trail 变体对比(内存现算参数是临时变体, 复用即错误)。
INSERT INTO config (key, value) VALUES ('backtest_reuse_days', '7')
ON CONFLICT (key) DO NOTHING;
