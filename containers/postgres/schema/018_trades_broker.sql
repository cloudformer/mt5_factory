-- 018_trades_broker.sql — trades 加券商列(券商是成交的事实; 系统流水按"券商→账号"两级过滤)
-- 存库比"查询时 join mt5_hosts 派生"强壮: 账号换机/worker 离线后, 券商归属仍在库里。
ALTER TABLE trades ADD COLUMN IF NOT EXISTS broker VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_trades_broker ON trades (broker, account);
