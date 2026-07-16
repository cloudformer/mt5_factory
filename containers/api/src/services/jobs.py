"""jobs 队列(schema/020): 数据库即任务队列 — 投递 / SKIP LOCKED 消费 / 租约回收

还铁律欠账"批量回测队列+进度在 api 进程内存":
- 投递后 api 重启批次不丢, consumer 起来接着跑(断点续跑)
- 进度 = 查表聚合, 任何副本都能答
- 消费用 FOR UPDATE SKIP LOCKED 抢单: 多副本并发安全, 天然负载均衡, 不需要选主
- 按品种排序抢单 + 消费侧缓存最近品种的 M1(加载最贵), 同品种任务连续命中缓存
"""
import asyncio
import logging
import os
import socket
from datetime import datetime, timezone

import asyncpg

from src.services import backtest

logger = logging.getLogger("jobs")

KIND = "backtest"            # 目前唯一任务类型
POLL_SECONDS = 3             # 队列空时的轮询间隔
LEASE_MINUTES = 30           # RUNNING 超时视为消费者死单, 扫回重试(单个回测秒级, 30分钟很宽)
MAX_ATTEMPTS = 2             # 含首跑; 超过则 FAILED(错误留在行里可查)
WORKER = f"{socket.gethostname()}:{os.getpid()}"


async def submit_batch(pool: asyncpg.Pool, items: list[dict]) -> int:
    """新批次投递: 删光旧批次(自清理, 铁律3 — 表内只留最新一批) + 整批插入。
    并行批次由调用方(routes)先查 has_active 拒绝, 这里不重复把关。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM jobs WHERE kind = $1", KIND)
            await conn.executemany(
                "INSERT INTO jobs (kind, payload) VALUES ($1, $2)",
                [(KIND, it) for it in items])
    return len(items)


async def has_active(pool: asyncpg.Pool) -> bool:
    return await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM jobs WHERE kind=$1"
        " AND status IN ('PENDING','RUNNING'))", KIND)


async def progress(pool: asyncpg.Pool) -> dict:
    """进度聚合(与旧 bt_state 同结构, web 零改动):
    {running, current, done, total, errors}"""
    rows = await pool.fetch(
        "SELECT status, count(*) AS n FROM jobs WHERE kind=$1 GROUP BY status", KIND)
    n = {r["status"]: r["n"] for r in rows}
    total = sum(n.values())
    done = n.get("DONE", 0) + n.get("FAILED", 0)
    current = None
    if n.get("RUNNING"):
        p = await pool.fetchval(
            "SELECT payload FROM jobs WHERE kind=$1 AND status='RUNNING'"
            " ORDER BY started_at DESC LIMIT 1", KIND)
        if p:
            current = f"{p.get('name', p.get('strategy_id'))} @ {p.get('symbol')}"
    errors = [f"{r['payload'].get('name')} @ {r['payload'].get('symbol')}: {r['error']}"
              for r in await pool.fetch(
                  "SELECT payload, error FROM jobs WHERE kind=$1 AND status='FAILED'"
                  " ORDER BY id LIMIT 50", KIND)]
    return {"running": (n.get("PENDING", 0) + n.get("RUNNING", 0)) > 0,
            "current": current, "done": done, "total": total, "errors": errors}


async def _reclaim(pool: asyncpg.Pool):
    """租约回收: RUNNING 超时的死单 → 未超次数扫回 PENDING 重试, 超了标 FAILED"""
    n = await pool.execute(
        "UPDATE jobs SET"
        "   status = CASE WHEN attempts >= $2 THEN 'FAILED' ELSE 'PENDING' END,"
        "   error  = CASE WHEN attempts >= $2"
        "            THEN coalesce(error || ' | ', '') || 'lease expired' ELSE error END,"
        "   finished_at = CASE WHEN attempts >= $2 THEN now() ELSE NULL END"
        " WHERE kind=$3 AND status='RUNNING'"
        "   AND started_at < now() - make_interval(mins => $1)",
        LEASE_MINUTES, MAX_ATTEMPTS, KIND)
    if n != "UPDATE 0":
        logger.warning("reclaimed stale jobs: %s", n)


async def _run_one(pool: asyncpg.Pool, payload: dict, cache: dict):
    """执行一个 backtest job(策略×品种)。策略/品种/配置临跑现查(最新);
    M1 按 (品种,时间窗) 缓存在消费者内存 — 抢单按品种排序, 同品种连续命中。"""
    sym = payload["symbol"]
    t_from = datetime.fromisoformat(payload["from"])
    t_to = datetime.fromisoformat(payload["to"])
    s = await pool.fetchrow(
        "SELECT id, name, template, params, timeframe FROM strategies WHERE id=$1",
        payload["strategy_id"])
    if s is None:
        raise ValueError("strategy deleted")
    meta = await pool.fetchrow("SELECT point, broker FROM symbols WHERE symbol=$1", sym)
    if meta is None:
        raise ValueError("symbol not in symbols table")
    key = (sym, payload["from"], payload["to"])
    if cache.get("key") != key:
        cache["m1"] = await backtest.load_m1(pool, sym, t_from, t_to)
        cache["key"] = key
    if cache["m1"] is None:
        raise ValueError(f"no M1 data for {sym}, run /syncdata first")
    oos_split = await pool.fetchval(
        "SELECT value FROM config WHERE key='backtest_oos_split'") or 0.7
    result = await asyncio.to_thread(
        backtest.run_backtest, cache["m1"], s["template"], s["params"],
        meta["point"], s["timeframe"], oos_split=oos_split, **payload["costs"])
    # 每"策略×品种"一行, upsert 覆盖(幂等 — job 重试安全, 铁律6)
    await pool.execute(
        "INSERT INTO backtests"
        " (strategy_id, from_time, to_time, symbol, broker, metrics, trades)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7)"
        " ON CONFLICT (strategy_id, symbol) DO UPDATE SET"
        "   from_time=EXCLUDED.from_time, to_time=EXCLUDED.to_time,"
        "   broker=EXCLUDED.broker, metrics=EXCLUDED.metrics,"
        "   trades=EXCLUDED.trades, created_at=now()",
        s["id"], t_from, t_to, sym, meta["broker"], result["metrics"], result["trades"])


async def consumer_loop(pool: asyncpg.Pool):
    """常驻消费者(api 启动即跑; 将来 worker 容器复用同一函数):
    抢单(SKIP LOCKED, 按品种排序) → 执行 → DONE/FAILED; 空队列时低频轮询 + 顺手回收租约。"""
    cache: dict = {}
    logger.info("jobs consumer started (%s)", WORKER)
    while True:
        try:
            await _reclaim(pool)
            job = await pool.fetchrow(
                "UPDATE jobs SET status='RUNNING', worker=$1,"
                "   started_at=now(), attempts=attempts+1"
                " WHERE id = (SELECT id FROM jobs WHERE kind=$2 AND status='PENDING'"
                "             ORDER BY payload->>'symbol', id LIMIT 1"
                "             FOR UPDATE SKIP LOCKED)"
                " RETURNING id, payload, attempts", WORKER, KIND)
            if job is None:
                cache.clear()   # 队列空: 释放缓存的 M1(可能几百MB), 再睡
                await asyncio.sleep(POLL_SECONDS)
                continue
            try:
                await _run_one(pool, job["payload"], cache)
                await pool.execute(
                    "UPDATE jobs SET status='DONE', error=NULL, finished_at=now()"
                    " WHERE id=$1", job["id"])
            except Exception as e:
                logger.error("job %s failed (attempt %d): %s", job["id"], job["attempts"], e)
                await pool.execute(
                    "UPDATE jobs SET status = CASE WHEN attempts >= $2"
                    "                         THEN 'FAILED' ELSE 'PENDING' END,"
                    "   error=$3, finished_at = CASE WHEN attempts >= $2"
                    "                           THEN now() ELSE NULL END"
                    " WHERE id=$1", job["id"], MAX_ATTEMPTS, str(e)[:500])
        except Exception as e:   # 池级/未知异常: 不让消费者死, 退避后重来
            logger.warning("consumer loop error: %s", e)
            await asyncio.sleep(10)
