"""回测 worker 入口 — 只跑 jobs 消费循环(无 HTTP / 无 schema / 无心跳)。

与 api 同一镜像同一代码, 只是 docker compose 里 worker 服务的 command 指到这里:
`--scale worker=N` 即 N 路并行消费。正确性全靠数据库(铁律 5/6):
SKIP LOCKED 抢单互不重复, 副本崩了租约回收整单回队重跑, 结果 UPSERT 幂等 —
worker 本身零状态, 加减副本/随时杀掉都不脏数据。
schema 由 api 启动执行(advisory lock 串行), compose 用 depends_on api healthy
保证 worker 起来时表已就绪, 这里不重复执行。
"""
import asyncio
import logging

import asyncpg

# 复用 api 的装配(配置只在一处): env 校验 / DSN / jsonb codec 全部同源, 不抄第二份
from src.main import DATABASE_URL, DB_URL_MASKED, _init_conn
from src.services import jobs

logger = logging.getLogger("worker")


async def main() -> None:
    for attempt in range(1, 6):   # 带重试等 postgres 就绪(与 api 同节奏)
        try:
            pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=1, max_size=3, init=_init_conn)
            logger.info("worker pool ready: %s", DB_URL_MASKED)
            break
        except Exception as e:
            logger.warning("DB connect attempt %d failed: %s", attempt, e)
            if attempt == 5:
                raise
            await asyncio.sleep(3)
    await jobs.consumer_loop(pool)   # 常驻; 进程被杀由 compose restart 拉起


if __name__ == "__main__":
    asyncio.run(main())
