-- 050_worker_params_throttle.sql — 下载节流参数(2026-07-29 与 Frank 定):
-- 首灌 20 年历史实测 worker 满速拉到 ~400万根后 Windows CPU 100%、web 卡 —
-- 每拉 dl_rest_bars 根休息 dl_rest_secs 秒(休息在锁外, 心跳趁隙插队, 顺带治
-- 深历史下载期间的假离线)。dl_rest_bars=0 = 不休息(小批增量同步用不到节流)。
-- 幂等: 只给还没有这两个键的存量行补默认值, 已调过的用户值不动。
UPDATE config
   SET value = value || '{"dl_rest_bars": 1000000, "dl_rest_secs": 30}'::jsonb
 WHERE key = 'worker_params' AND NOT value ? 'dl_rest_bars';
