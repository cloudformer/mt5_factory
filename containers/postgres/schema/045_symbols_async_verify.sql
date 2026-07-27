-- 045_symbols_async_verify.sql — 品种登记改异步校验(2026-07-26, v7.2 单向化 #7)
-- 旧流程: 登记时 api 反向连下载 worker 当场问券商(api→worker, 单向化要消灭的方向)。
-- 新流程: 登记先入库(verified_at=NULL 待校验) → announce 应答下发校验任务 →
--         bridge 查本机 MT5, 下次 announce 捎回 → api 补齐精度/标失败。
-- digits/point 待校验期为空(校验通过才有值); 下游本就以 point 为准入(空=不可回测/不下载)。
ALTER TABLE symbols ALTER COLUMN digits DROP NOT NULL;
ALTER TABLE symbols ALTER COLUMN point DROP NOT NULL;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS verify_error TEXT;  -- 校验失败原因(如"券商没有该品种"); 重新登记即清空重试
