-- 061_oos_v2.sql — v0.6 OOS 筛选 v2(Regime OOS Screen v2, 2026-08-06 与 Frank 定稿)
-- 自动初筛器: 每策略只跑一次 20 年回测, 按锚点(跑批当天 UTC 0点)切三期六段(训练/测试),
-- 六段 PF 全合格 = PASS(无亏损段 ∞ 恒过, 0 笔段无数据不追责算过), 只出报告默认不动策略。
-- 插件式可移除 = DROP TABLE oos_v2_screens + DELETE FROM config WHERE key='oos_v2'
-- (basis 标签 oos_v2#<id> 是履历, 保留)。设计: docs/2.regime_dirction/v0.6_OOS筛选v2设计.md
CREATE TABLE IF NOT EXISTS oos_v2_screens (
    id         SERIAL PRIMARY KEY,       -- 报告号: basis 标签「oos_v2#<id>」溯源到本行
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode       VARCHAR(8)  NOT NULL CHECK (mode IN ('preview', 'execute')),
    anchor     DATE        NOT NULL,     -- 锚点 A = 跑批当天(全批共用一把刀, 批内可比)
    scope      JSONB       NOT NULL,     -- {"task", "ids" 或默认全池}
    params     JSONB       NOT NULL,     -- 判据快照(段定义+default_pf+min_seg_trades, 报告自解释)
    summary    JSONB       NOT NULL,     -- {total, passed, failed, skipped, not_run, warned}
                                         --   warned = 带「样本不足」警示(0笔段或<min_seg_trades)
                                         --   skipped = 任务失败/缺回测行(铁则1, 永不归档)
    details    JSONB       NOT NULL,     -- 逐策略: 各期 train/test {n,net,pf,dd} + verdict + reason
    owner_id   INTEGER
);

-- 判据种子(config 唯一源, 页面可改): 三期六段 + 两层 PF 门槛(每期 min_pf=null → 用 default_pf;
-- 未来可长0.8/中1.0/短1.2 就地生效不改代码)。段 = [起,止] 距锚点年数(年=365.25天), 0=当天。
INSERT INTO config (key, value)
VALUES ('oos_v2', '{
  "segments": [
    {"name": "long",   "label": "长期", "train": [20, 5],   "test": [5, 0],   "min_pf": null},
    {"name": "medium", "label": "中期", "train": [5, 1.5],  "test": [1.5, 0], "min_pf": null},
    {"name": "short",  "label": "短期", "train": [2, 0.5],  "test": [0.5, 0], "min_pf": null}
  ],
  "default_pf": 1.0,
  "min_seg_trades": 10,
  "batch_limit": 50
}')
ON CONFLICT (key) DO NOTHING;
