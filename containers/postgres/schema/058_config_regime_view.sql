-- 058_config_regime_view.sql — Regime 页默认视图(2026-08-04 与 Frank 定):
-- 色带图示跨度 band_years(0=全部) + 八格象限时间窗口 quad_years(0=全历史, 负=N年以前)。
-- 全局共享的展示默认(视图是全局的, owner=admin): 配置页仅 admin 可改, 页面上临时切换不落库。
INSERT INTO config (key, value)
VALUES ('regime_view', '{"band_years": 3, "quad_years": 0}')
ON CONFLICT (key) DO NOTHING;
