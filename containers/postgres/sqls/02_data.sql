-- 02_data.sql - 数据层: 历史K线(按年分区)
-- 所有回测/模拟都基于这些从 MT5 下载的真实数据

-- 字段对齐 MT5 copy_rates_* 返回结构
CREATE TABLE historical_bars (
    symbol      VARCHAR(32)      NOT NULL,
    timeframe   VARCHAR(8)       NOT NULL,  -- M1/M5/M15/M30/H1/H4/D1/W1/MN1
    time        TIMESTAMPTZ      NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    tick_volume BIGINT           NOT NULL DEFAULT 0,
    spread      INTEGER          NOT NULL DEFAULT 0,
    real_volume BIGINT           NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, timeframe, time)
) PARTITION BY RANGE (time);

-- 42 个分区全覆盖, 任何时间写入都不报错:
--   pre2000 (最早→2000) + 2000~2039 共40个年度分区 + post2040 (2040→最晚)
CREATE TABLE historical_bars_pre2000 PARTITION OF historical_bars
    FOR VALUES FROM (MINVALUE) TO ('2000-01-01');

DO $$
DECLARE
    y INTEGER;
BEGIN
    FOR y IN 2000..2039 LOOP
        EXECUTE format(
            'CREATE TABLE historical_bars_%s PARTITION OF historical_bars
             FOR VALUES FROM (%L) TO (%L)',
            y, make_date(y, 1, 1), make_date(y + 1, 1, 1)
        );
    END LOOP;
END $$;

CREATE TABLE historical_bars_post2040 PARTITION OF historical_bars
    FOR VALUES FROM ('2040-01-01') TO (MAXVALUE);

SELECT '02_data.sql done' AS status;
