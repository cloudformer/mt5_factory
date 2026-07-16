-- 019_strategy_runtime.sql — 策略运行区间(状态表, 非日志): 一行 = 一段连续真实运行
--
-- 写入: api 心跳(services/sync)看到 worker 的 per_strategy 名单后, 按 worker 批量
--       "推进最近区间的 run_to / 或新开一段"。runner/bridge 零改动, 没有第二个写入方。
--       死机/断电/下架 = 心跳停 = run_to 自动定格在最后活着的时刻, 不存在悬空未关闭的段。
-- 读取: 关2对账按区间分段取回测信号 —— 区间外(策略没在跑)的回测信号不参与对账不扣分;
--       区间内"回测有信号实盘没单"照样抓 runner 漏单。"时刻t在跑吗" = 一个范围查询。
CREATE TABLE IF NOT EXISTS strategy_runtime (
    strategy_id INTEGER      NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    run_from    TIMESTAMPTZ  NOT NULL,   -- 段起点: 心跳第一次见到它在跑
    run_to      TIMESTAMPTZ  NOT NULL,   -- 段终点: 心跳最近一次见到它在跑(滞后≤写入间隔)
    host        VARCHAR(64),             -- 跑在哪台 worker(排查 worker 稳定性用)
    PRIMARY KEY (strategy_id, run_from)
);
-- 对账按 (strategy_id, 时间范围) 查段; run_to 上的索引让"最近一段"走索引
CREATE INDEX IF NOT EXISTS idx_strategy_runtime_to ON strategy_runtime (strategy_id, run_to);

-- 节奏参数进 config(配置页·回测参数可改):
-- runtime_write_minutes: 策略运行状态写入间隔(分钟)。心跳30秒一次, 但 run_to 落后超过
--                        本值才真正写库(节流); 死机定格误差 ≤ 本值, 对账容差±20分钟, 5足够。
-- runtime_gap_minutes:   裂段阈值(分钟)。距上次 run_to 超过本值才算新的一段;
--                        必须明显大于写入间隔, 否则正常节流会被误判成断线。短于本值的
--                        停机会被连成一段(方向偏严格: 多比对, 不漏比)。
INSERT INTO config (key, value) VALUES ('runtime_write_minutes', '5') ON CONFLICT (key) DO NOTHING;
INSERT INTO config (key, value) VALUES ('runtime_gap_minutes', '15') ON CONFLICT (key) DO NOTHING;
