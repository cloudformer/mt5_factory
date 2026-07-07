"""数据同步与心跳 — services 层: 下载M1(多worker并行分摊)、心跳状态机、host事件"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx

logger = logging.getLogger("sync")

BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")
CHUNK_DAYS = 30  # M1 每次拉 30 天 ≈ 4.3万根, 低于 bridge 单次上限

# 全局同步状态 (单进程内存即可, 不用太复杂)
state = {"running": False, "current": {}, "symbols": [],
         "bars_written": 0, "done": [], "errors": []}


async def load_sync_config(pool: asyncpg.Pool) -> tuple[list, datetime]:
    """品种清单和起始日期来自 config 表 (web/API 可改)"""
    cfg = {r["key"]: r["value"] for r in await pool.fetch("SELECT key, value FROM config")}
    symbols = cfg.get("symbols") or []
    data_start = datetime.fromisoformat(str(cfg.get("data_start") or "2015-01-01")).replace(
        tzinfo=timezone.utc)
    return symbols, data_start


async def _download_hosts(pool: asyncpg.Pool):
    """所有可用的下载 worker — 多台并行下载, 品种轮询分摊"""
    return await pool.fetch(
        "SELECT name, host, port FROM mt5_hosts"
        " WHERE enabled AND download ORDER BY id"
    )


async def _insert_bars(conn: asyncpg.Connection, symbol: str, bars: list) -> int:
    records = [
        (symbol, "M1", datetime.fromtimestamp(b["time"], tz=timezone.utc),
         b["open"], b["high"], b["low"], b["close"],
         b["tick_volume"], b["spread"], b["real_volume"])
        for b in bars
    ]
    async with conn.transaction():
        await conn.execute("CREATE TEMP TABLE _stage (LIKE historical_bars) ON COMMIT DROP")
        await conn.copy_records_to_table("_stage", records=records)
        result = await conn.execute(
            "INSERT INTO historical_bars SELECT * FROM _stage ON CONFLICT DO NOTHING"
        )
    return int(result.split()[-1])  # "INSERT 0 N" -> N


async def _sync_symbol(pool: asyncpg.Pool, client: httpx.AsyncClient, base: str,
                       symbol: str, data_start: datetime, worker: str):
    # 断点续传: 从库里最后一根 bar 继续
    last = await pool.fetchval(
        "SELECT max(time) FROM historical_bars WHERE symbol=$1 AND timeframe='M1'", symbol
    )
    cursor = last or data_start
    now = datetime.now(timezone.utc)

    while cursor < now:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), now)
        state["current"][worker] = f"{symbol} {cursor:%Y-%m-%d}"
        resp = await client.get(f"{base}/rates", params={
            "symbol": symbol, "timeframe": "M1",
            "from_ts": int(cursor.timestamp()), "to_ts": int(chunk_end.timestamp()),
        })
        resp.raise_for_status()
        bars = resp.json()["bars"]
        if bars:
            async with pool.acquire() as conn:
                written = await _insert_bars(conn, symbol, bars)
            state["bars_written"] += written
        cursor = chunk_end
    logger.info("%s synced (via %s)", symbol, worker)


async def _worker_sync(pool: asyncpg.Pool, client: httpx.AsyncClient,
                       host, symbols: list, data_start: datetime):
    """一台 worker 串行下载分给它的品种 (bridge 内部 MT5 调用本就串行)"""
    base = f"http://{host['host']}:{host['port']}"
    for symbol in symbols:
        try:
            await _sync_symbol(pool, client, base, symbol, data_start, host["name"])
            state["done"].append(symbol)
        except Exception as e:
            logger.error("sync %s via %s failed: %s", symbol, host["name"], e)
            state["errors"].append(f"{symbol}@{host['name']}: {e}")
    state["current"].pop(host["name"], None)


async def run_full_sync(pool: asyncpg.Pool):
    """全量/增量同步: 品种轮询分摊到所有下载 worker, 并行执行"""
    hosts = await _download_hosts(pool)
    if not hosts:
        state["errors"].append("no enabled mt5_host with role 'download'")
        state["running"] = False
        return

    symbols, data_start = await load_sync_config(pool)
    if not symbols:
        state["errors"].append("config.symbols is empty")
        state["running"] = False
        return

    headers = {"X-API-Key": BRIDGE_API_KEY} if BRIDGE_API_KEY else {}
    state.update(current={}, symbols=symbols, bars_written=0, done=[], errors=[])
    # 轮询分摊: worker i 负责 symbols[i::n]
    assignments = [(h, symbols[i::len(hosts)]) for i, h in enumerate(hosts)]
    logger.info("sync across %d workers: %s", len(hosts),
                {h["name"]: syms for h, syms in assignments})

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        await asyncio.gather(*(
            _worker_sync(pool, client, h, syms, data_start)
            for h, syms in assignments if syms
        ))
    state["running"] = False
    state["current"] = {}
    logger.info("full sync finished: %s bars, errors=%s", state["bars_written"], state["errors"])


async def log_host_event(pool: asyncpg.Pool, host_id: int, event: str, detail: dict | None = None):
    """worker 生命周期事件入库 (追踪用)"""
    await pool.execute(
        "INSERT INTO mt5_host_events (host_id, event, detail) VALUES ($1, $2, $3)",
        host_id, event, detail or {})


async def heartbeat_loop(pool: asyncpg.Pool):
    """每 30s 轮询启用 worker 的 /health, 维护 ONLINE/OFFLINE 状态机 + 事件记录。
    下线判定带 90s 宽限, 避免单次超时抖动。"""
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                hosts = await pool.fetch(
                    "SELECT id, name, host, port, status FROM mt5_hosts WHERE enabled")
                for h in hosts:
                    alive = False
                    try:
                        r = await client.get(f"http://{h['host']}:{h['port']}/health")
                        alive = r.status_code == 200 and r.json().get("status") == "healthy"
                    except httpx.HTTPError:
                        alive = False

                    if alive:
                        if h["status"] == "OFFLINE":  # 离线→上线
                            await pool.execute(
                                "UPDATE mt5_hosts SET status='ONLINE', online_at=now(),"
                                " last_heartbeat=now() WHERE id=$1", h["id"])
                            await log_host_event(pool, h["id"], "ONLINE")
                            logger.info("worker %s ONLINE", h["name"])
                        else:
                            await pool.execute(
                                "UPDATE mt5_hosts SET last_heartbeat=now() WHERE id=$1", h["id"])
                    else:  # 探测失败: 超过90s宽限才判下线
                        row = await pool.fetchrow(
                            "UPDATE mt5_hosts SET status='OFFLINE', offline_at=now()"
                            " WHERE id=$1 AND status='ONLINE'"
                            "   AND (last_heartbeat IS NULL OR"
                            "        last_heartbeat < now() - interval '90 seconds')"
                            " RETURNING id", h["id"])
                        if row:
                            await log_host_event(pool, h["id"], "OFFLINE")
                            logger.warning("worker %s OFFLINE", h["name"])
            except Exception as e:
                logger.warning("heartbeat loop error: %s", e)
            await asyncio.sleep(30)
