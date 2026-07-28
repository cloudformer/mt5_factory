-- 048_config_regime_params.sql — Regime 口径进配置(2026-07-27, v2.5 评定期)
-- 评定期在配置页切换候选(sma200/ema200/ema50...), 保存后触发全量重算**覆盖更新**
-- (UPSERT 同主键覆盖 + 修剪新暖机起点前的头部残留), 页面记分卡随之出新打分。
-- 纪律不变: 禁止用策略盈利调口径; 评定完成后选定值冻结并写回 v2.5 文档。
INSERT INTO config (key, value) VALUES ('regime_params', '{
  "long_ma": "sma200",
  "short_ma": "sma20",
  "atr_n": 14,
  "vol_win": 252,
  "vol_q": 0.5
}') ON CONFLICT (key) DO NOTHING;
