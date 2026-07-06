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

-- 按年建分区: 2000 - 2036
DO $$
DECLARE
    y INTEGER;
BEGIN
    FOR y IN 2000..2036 LOOP
        EXECUTE format(
            'CREATE TABLE historical_bars_%s PARTITION OF historical_bars
             FOR VALUES FROM (%L) TO (%L)',
            y, make_date(y, 1, 1), make_date(y + 1, 1, 1)
        );
    END LOOP;
END $$;

SELECT '02_data.sql done' AS status;
