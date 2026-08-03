-- 056_regime_screen.sql — v0.5 Regime 筛选(批量粗筛 + 落库报告, 2026-08-03 与 Frank 定稿)
-- 测试性质可插拔功能(自有代码 = api/routes/regime_screen.py + web 页面);
-- 移除 = DROP TABLE regime_screens + DELETE FROM config WHERE key='regime_screen'
-- (死因码 regime_unstable 与 basis 标签保留 — 历史履历)。
CREATE TABLE IF NOT EXISTS regime_screens (
    id         SERIAL PRIMARY KEY,       -- 报告号: basis 标签「regime筛过#<id>」溯源到本行
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode       VARCHAR(8)  NOT NULL CHECK (mode IN ('preview', 'execute')),
    version_id INTEGER     NOT NULL,     -- 判定用 regime 版本(纯记录不 FK: 版本删了报告仍是履历)
    scope      JSONB       NOT NULL,     -- {"label" 或 "ids", "symbols": "main"/"all"}
    params     JSONB       NOT NULL,     -- 判据快照 {"boundaries_years", "min_cell_trades"}
    summary    JSONB       NOT NULL,     -- {"total","passed","failed","archived","skipped"}
    details    JSONB       NOT NULL      -- 逐策略明细(id/窗口/笔数/各切分合格格/通过格/结论)
);

-- 判据参数种子(config 唯一源, 页面可改): 按年切四刀 + 格内 5 笔地板(粗筛防掷硬币, 改 1≈关闭)
INSERT INTO config (key, value)
VALUES ('regime_screen', '{"boundaries_years": [1, 2, 3, 4], "min_cell_trades": 5}')
ON CONFLICT (key) DO NOTHING;
