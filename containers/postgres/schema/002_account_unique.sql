-- 002_account_unique.sql — 铁律: 不同 worker 不得共用同一 MT5 账户 (同账户双跑 = 重复下单)
--
-- 数据库唯一索引是本铁律的唯一执法者, 代码各处只做"把账户写进列, 写失败=撞号":
--   /connect 下发账户    → 先占位, 撞号 409, 不碰 bridge   (routes/hosts.py _claim_account)
--   指派 demo/live 职能  → 同步实际登录账户, 撞号 409       (routes/hosts.py update_host)
--   心跳                → 同步实际登录账户, 撞号仅告警      (services/sync.py _beat_one)
--
-- 只对 enabled 主机生效, 语义刻意如此:
--   掉线   → 占用保留 (掉线可能是抖动, 释放会导致恢复后与新机双跑)
--   停用/删除 → 释放账户 (人的明确决定, 接管流程第一步)
--   重新启用 → 数据库自动重查, 撞号则拒绝启用
CREATE UNIQUE INDEX IF NOT EXISTS uniq_mt5_hosts_account
    ON mt5_hosts (mt5_login, mt5_server)
    WHERE enabled AND mt5_login IS NOT NULL;
