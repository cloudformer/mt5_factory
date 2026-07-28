-- 047_regime_timeline.sql — 市场状态时间线(v2.5 Regime v1, 2026-07-27 与 Frank 定)
-- 一品种一天一行; 三个维度各自一列(可单维查询/统计), 汇总格子 regime 用**生成列**拼出 —
-- 一致性由数据库执法, 三列与汇总永不可能对不上。
-- 派生数据: 从 D1(M1 聚合)算出, 读时自愈补算(services/regime.py), UPSERT 覆盖更新
-- (换口径=全量重算覆盖, 不删数据; 仅暖机起点后移时修剪头部残留行, 保持干净)。
-- 尘埃级增长: 9品种×365天≈3千行/年。
CREATE TABLE IF NOT EXISTS regime_timeline (
    symbol      VARCHAR(32) NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
    date        DATE        NOT NULL,  -- 券商服务器时间的交易日
    long_trend  CHAR(1)     NOT NULL CHECK (long_trend  IN ('A', 'B')),  -- A牛/B熊
    short_trend CHAR(1)     NOT NULL CHECK (short_trend IN ('A', 'B')),  -- A牛/B熊
    vol         CHAR(1)     NOT NULL CHECK (vol         IN ('A', 'B')),  -- A高波/B低波
    regime      CHAR(3)     GENERATED ALWAYS AS (long_trend || short_trend || vol) STORED,
    PRIMARY KEY (symbol, date)
);
