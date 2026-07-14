-- 012_config_ranking_templates.sql — 排名模板进 config(UI 可增删改, 不写死代码)
--
-- 模板 = 四维权重(稳定=PF/盈利=净点/风险=回撤小/健壮=跨品种盈利比) + 默认最少笔数。
-- 排名时各维先归一成 0~100 排名百分位再加权(消除量纲), 权重按比例归一(不必和=100)。
-- min_trades 因模板而异: 看 PF/回撤的要大样本(如300), 看盈利的100即可 — 全部配置页可调。
INSERT INTO config (key, value) VALUES ('ranking_templates', '[
  {"name": "稳定性", "stable": 50, "profit": 10, "risk": 20, "robust": 20, "min_trades": 300},
  {"name": "盈利",   "stable": 10, "profit": 50, "risk": 10, "robust": 30, "min_trades": 100},
  {"name": "风险",   "stable": 30, "profit": 10, "risk": 40, "robust": 10, "min_trades": 200}
]') ON CONFLICT (key) DO NOTHING;
