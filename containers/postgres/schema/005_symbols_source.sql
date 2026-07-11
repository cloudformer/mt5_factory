-- 005_symbols_source.sql — symbols 表升级为"品种唯一数据源"
--
-- 病根(2026-07 BTCUSD 事件): 品种信息散在三处且互不校验 —
--   config.symbols(下载哪些) / symbols表(精度) / 券商(真实可用性), 手填 point 靠猜, 全局 data_start 一刀切。
-- 修复: symbols 表成为唯一源, 一切(下载/回测/策略生成)只读它;
--   品种登记时向券商校验并自动取真实精度(见 api POST /symbols), config.symbols/data_start 废弃。
--
-- 新增列:
--   download    是否参与数据下载 (取代 config.symbols)
--   role        trade(交易品种) / validate(跨品种反过拟合验证) — CLAUDE.md 两类品种
--   data_start  每品种独立起始日期 (BTCUSD 没有 2015 数据 → 各管各的, 取代全局 config.data_start)
--   volume_min / stops_level  券商下单约束 (登记时自动取, 回测/下单可用)
--   verified_at 最后一次对券商校验成功的时间 (NULL = 未经券商确认, 手工塞进来的)
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS download    BOOLEAN     NOT NULL DEFAULT TRUE;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS role        VARCHAR(8)  NOT NULL DEFAULT 'trade'
    CHECK (role IN ('trade', 'validate'));
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS data_start  DATE        NOT NULL DEFAULT '2015-01-01';
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS volume_min  DOUBLE PRECISION;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS stops_level INTEGER;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

-- 把旧的全局 data_start 迁到每个品种上 (仅首次: 只覆盖仍是默认值的行)
UPDATE symbols s SET data_start = (c.value #>> '{}')::date
  FROM config c
 WHERE c.key = 'data_start' AND s.data_start = '2015-01-01'
   AND (c.value #>> '{}') ~ '^\d{4}-\d{2}-\d{2}$';

-- 验证品种标注 role (CLAUDE.md 默认验证品种); 其余保持 trade
UPDATE symbols SET role = 'validate'
 WHERE symbol IN ('AUDUSD', 'USDCAD', 'NZDUSD', 'EURJPY', 'GBPJPY') AND role = 'trade';

-- 清理: 2026-07 事件里手填的 BTCUSD (point 靠猜, 从未经券商校验) —
-- 新流程下应通过 POST /symbols 由券商校验后重新登记
DELETE FROM symbols WHERE symbol = 'BTCUSD' AND verified_at IS NULL;

-- 废弃 config 里的 symbols / data_start (改由 symbols 表管; 留着不删避免老代码读到 null 报错, 但不再使用)
