-- 021_config_cross_gate.sql — 交叉测试门槛进 config(配置页可改)
-- 批量回测勾"跨品种"时: 策略的【主品种】成绩过此门槛才展开交叉品种, 否则只跑主品种
-- (主货币都不行不配测健壮性, 省 ~90% 交叉算力)。按 ID 点名的回测不走门槛(点名即信任)。
-- 每项可为 null = 不检查; PF 为 null(零亏损=∞)视为通过。默认值故意宽泛, 配置页收紧。
INSERT INTO config (key, value) VALUES ('cross_symbol_gate',
  '{"min_trades": 20, "min_win_rate": 0.3, "min_net_points": 0, "min_pf": 1.0, "max_dd_points": null}')
ON CONFLICT (key) DO NOTHING;
