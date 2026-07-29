-- 052_regime_eval_cache.sql — 候选口径族评分缓存(2026-07-29 与 Frank 定):
-- 候选族对比页每次全算(8口径×全品种×20年D1)要几十秒 → 按口径存一份, 加载即读缓存,
-- 手动"重算"才现算覆盖。评分是调试期只读产物, 不进交易/归因链路 —
-- 换配置/加品种也无残留可言(缓存过期=手动重算即可, 从不自动污染)。
-- 主键=口径规范串(长|短|atr|窗|分位), UPSERT 覆盖, 免维护(铁律3)。
CREATE TABLE IF NOT EXISTS regime_eval_cache (
  params_key   TEXT PRIMARY KEY,               -- 'sma200|sma20|14|252|0.5'
  params       JSONB NOT NULL,                 -- 原始口径(回显用)
  symbols      TEXT[] NOT NULL,                -- 算这份时纳入的品种(变了=缓存旧, 页面标注)
  per_symbol   JSONB NOT NULL,                 -- {品种: {stats, distinct}}
  computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
