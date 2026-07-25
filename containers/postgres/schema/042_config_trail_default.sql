-- 042_config_trail_default.sql — 移动止损全局默认(v0.9, 2026-07-25)
-- 策略 params 没填 trail 时的回落值(volume_default 同构; 未来按 userid 走 user_config 覆盖)。
-- 种子 = null(全局关): 部署后所有回测/对账逐字节不变(v2.4 无配置=旧行为用例锁死)。
-- 结构见 strategy_core/trailing.py 模块头: {"active": "fixed"|"breakeven"|"atr"|null, ...}
INSERT INTO config (key, value) VALUES ('trail_default', 'null')
ON CONFLICT (key) DO NOTHING;
