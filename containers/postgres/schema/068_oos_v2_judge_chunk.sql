-- 068_oos_v2_judge_chunk.sql — 判定块大小定为 300(2026-08-08 全池压测定版)
-- 实测: 500/块 worker 峰值 2.6G 且收尾"两个啃尾巴八个空等"; 300/块 峰值 ~1.6G(贴 2G 预算),
-- 块小负载更均匀墙钟不变或略快, 且 worker 副本以后加多时打包效率更高。
-- 改法 = 改库(UI 无编辑口): UPDATE config SET value=jsonb_set(value,'{judge_chunk}','400') ...
UPDATE config SET value = jsonb_set(value, '{judge_chunk}', '300')
 WHERE key = 'oos_v2' AND (value->>'judge_chunk') IS DISTINCT FROM '300';
