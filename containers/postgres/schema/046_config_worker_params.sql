-- 046_config_worker_params.sql — worker 参数进配置(2026-07-26 与 Frank 定):
-- 上报节奏/批量等由用户按自己网络在配置页调, 唯一源=config 表;
-- 下发走"报到领任务"管道: announce 应答携带, worker 领回落本地文件, 1~2 分钟生效。
-- 注意: heartbeat_seconds 上限 60 — 轮询侧"新鲜推送"窗口 75s, 推得比它慢会推/拉来回抖。
INSERT INTO config (key, value) VALUES ('worker_params', '{
  "heartbeat_seconds": 30,
  "announce_seconds": 60,
  "bars_batch": 50000,
  "decision_keep_days": 14
}') ON CONFLICT (key) DO NOTHING;
