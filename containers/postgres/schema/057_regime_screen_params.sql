-- 057_regime_screen_params.sql — v0.5 判据扩展(2026-08-04 与 Frank 定):
-- 总计年(window_years) / 至少合格格数(min_pass_cells) / 净点阈值(min_net_points) / PF阈值(min_pf)
-- 全部可配, 默认值 = 原行为(5年 · ≥1格 · 净点>0 · PF>1, 后两者默认等价, 调高即收紧)。
-- 切分语义 = 近 b 年(后段) vs 剩余(前段), b 允许小数(0.5 / 3.8 / 4.5)。
-- 老库: || 合并新键(已有键以库内值为准, 不覆盖); 新库: 整包种子。幂等。
UPDATE config
   SET value = '{"window_years": 5, "min_pass_cells": 1, "min_net_points": 0, "min_pf": 1.0}'::jsonb
               || value
 WHERE key = 'regime_screen';

INSERT INTO config (key, value)
VALUES ('regime_screen',
        '{"window_years": 5, "boundaries_years": [1, 2, 3, 4], "min_cell_trades": 5,
          "min_pass_cells": 1, "min_net_points": 0, "min_pf": 1.0}')
ON CONFLICT (key) DO NOTHING;
