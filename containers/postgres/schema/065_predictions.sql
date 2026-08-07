-- 065_predictions.sql — 预测快照(v0.7 批次3, 2026-08-08 与 Frank 定)
-- 唯一必须落库的新数据: Expected 必须在冻结时刻定死(防泄露的本质)。
-- 冻结日 = 带门策略的生成日期(strategies.created_at, 天然存在不可篡改);
-- Expected = 冻结日前 expected_window_years(默认3年)的门内战绩, 首次计算后永不覆盖
-- (UNIQUE + 插入 ON CONFLICT DO NOTHING = 冻结执法)。
-- Actual 侧不落库: 冻结日之后的成交读时现拼(保持率/同期增益/成熟度门槛见 config prediction)。
CREATE TABLE IF NOT EXISTS predictions (
    id            SERIAL PRIMARY KEY,
    strategy_id   INTEGER     NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    frozen_at     TIMESTAMPTZ NOT NULL,   -- 冻结时刻 = 带门策略的 created_at
    state_version INTEGER     NOT NULL,   -- 门钉死的口径版本
    state_key     TEXT        NOT NULL,   -- 门格集合(如 "ABA·ABB")
    expected      JSONB       NOT NULL,   -- {pf, win_rate, net, n, dd, window_years} 冻结值
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, frozen_at)       -- 一个冻结时刻一份快照, 重算插不进来 = 冻结执法
);

-- 验证判据种子(config 唯一源): 成熟度门槛 + 保持率及格线 + Expected 回看窗
INSERT INTO config (key, value)
VALUES ('prediction', '{"expected_window_years": 3, "min_trades": 20,
                        "min_days": 90, "retention_ok": 0.8}')
ON CONFLICT (key) DO NOTHING;
