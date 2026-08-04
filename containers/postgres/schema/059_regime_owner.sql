-- 059_regime_owner.sql — Regime 资产归属(2026-08-04 与 Frank 定):
-- 1) regime_versions.owner_id: 版本记归属(现在只记不筛 — 全员可见公共尺;
--    操作收 admin 由页面门禁执法; 将来放开"用户自建版本"时逻辑直接成立)
-- 2) regime_screens.owner_id: 筛选报告记发起人 — owner 只见自己的报告, admin 全见
-- 存量回填 owner=admin(id 1), 与 strategies.owner_id(033) 同款。
ALTER TABLE regime_versions
    ADD COLUMN IF NOT EXISTS owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id);
ALTER TABLE regime_screens
    ADD COLUMN IF NOT EXISTS owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id);
