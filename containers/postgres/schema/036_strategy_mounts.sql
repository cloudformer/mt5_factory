-- 036_strategy_mounts.sql — v5.0-A 策略挂载表(建表+回填, 不激活)(2026-07-24)
-- 多挂载: 一策略可挂多台 worker 同时跑(不同账户, trades 主键含 account 天然分开;
-- magic=100000+id 只需单账户内唯一, 不变量不动)。env 由 host 角色带出, 单挂载=一行特例。
-- A 段只建表+回填, runner 仍按"角色全量"认领(B 段才改认领键+stats按账户+手数搬家),
-- 本表此时无人读 — 零行为变化。
CREATE TABLE IF NOT EXISTS strategy_mounts (
    strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    host_id     INTEGER NOT NULL REFERENCES mt5_hosts(id),
    volume      DOUBLE PRECISION,                -- 每挂载点独立手数(空=默认; B 段生效, 回填带入现值)
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (strategy_id, host_id)           -- 一策略在一台 worker 上最多挂一次
);

-- 一次性回填, 镜像当前行为: DEMO 策略挂到全部启用的 demo 机, LIVE 挂到 live 机
-- (现状就是同角色机器全量跑同一批)。空表守卫: 本文件每次 api 启动都重放,
-- 无守卫会把将来人工调整过的挂载偷偷补回来; NOT EXISTS 按语句快照求值, 回填原子完成。
INSERT INTO strategy_mounts (strategy_id, host_id, volume)
SELECT s.id, h.id, s.volume
FROM strategies s
JOIN mt5_hosts h ON h.runner = lower(s.status) AND h.enabled
WHERE s.status IN ('DEMO', 'LIVE')
  AND NOT EXISTS (SELECT 1 FROM strategy_mounts)
ON CONFLICT DO NOTHING;
