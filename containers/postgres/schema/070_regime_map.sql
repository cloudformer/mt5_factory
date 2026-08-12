-- 070_regime_map.sql — 筛选·策略×regime 映射规律(2026-08-11 与 Frank 定稿)
-- 问一件事: 这个策略的盈亏, 能不能被某个 regime 口径的八个格分出层次?
--   · 交易按 R 倍数(R = 该笔止损距离)分四类: 大赢>+2R / 小赢 0~+2R / 小亏 -1R~0 / 大亏<-1R
--   · 每个版本【独立】做一张 4类×8格 列联表 → 富集倍数 + 置换检验 p
--   · 铁律: 各版本独立评估, 【绝不跨版本比较】—— 不同版本同名格是不同的分类维度
--     (按性别分 vs 按上衣颜色分), 跨版本挑最好 = 数据挖掘, 分对了也是拟合(Frank 2026-08-11)
-- 插件式可移除 = DROP TABLE regime_map_screens(判据不入 config, 无残留)
CREATE TABLE IF NOT EXISTS regime_map_screens (
    id         SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    scope      JSONB       NOT NULL,   -- {task, ids/模板/品种/状态, limit}
    params     JSONB       NOT NULL,   -- 判据快照(四类口径/置换次数/信号阈值)
    summary    JSONB       NOT NULL,   -- {total, with_signal, weak, none, skipped}
    details    JSONB       NOT NULL,   -- 逐策略×版本: 四类占比 + 4×8表 + 富集 + p + 结论
    owner_id   INTEGER
);

-- 判据不落 config(2026-08-11 Frank 定): 探索阶段判据走页面表单每次现填,
-- 只随报告存快照(params 列) — 免得频繁调参污染全局配置; 稳定后再考虑挪进 config。
