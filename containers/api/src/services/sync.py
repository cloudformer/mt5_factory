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


async def load_download_symbols(pool: asyncpg.Pool) -> list:
    """要下载的品种及其独立起始日期 — 唯一来源 symbols 表 (download=TRUE)。
    返回 [{symbol, data_start(UTC datetime)}]; 每品种自己的起始日期(BTCUSD≠EURUSD)。"""
    rows = await pool.fetch(
        "SELECT symbol, data_start FROM symbols WHERE download ORDER BY symbol")
    return [{"symbol": r["symbol"],
             "data_start": datetime(r["data_start"].year, r["data_start"].month,
                                    r["data_start"].day, tzinfo=timezone.utc)}
            for r in rows]


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


async def _worker_sync(pool: asyncpg.Pool, client: httpx.AsyncClient, host, items: list):
    """一台 worker 串行下载分给它的品种 (bridge 内部 MT5 调用本就串行)。
    items: [{symbol, data_start}] — 每品种用自己的起始日期"""
    base = f"http://{host['host']}:{host['port']}"
    for it in items:
        try:
            await _sync_symbol(pool, client, base, it["symbol"], it["data_start"], host["name"])
            state["done"].append(it["symbol"])
        except Exception as e:
            logger.error("sync %s via %s failed: %s", it["symbol"], host["name"], e)
            state["errors"].append(f"{it['symbol']}@{host['name']}: {e}")
    state["current"].pop(host["name"], None)


async def run_full_sync(pool: asyncpg.Pool):
    """全量/增量同步: 品种轮询分摊到所有下载 worker, 并行执行。品种源 = symbols 表"""
    hosts = await _download_hosts(pool)
    if not hosts:
        state["errors"].append("no enabled mt5_host with role 'download'")
        state["running"] = False
        return

    items = await load_download_symbols(pool)
    if not items:
        state["errors"].append("没有开启下载的品种 — 在下载页登记品种(会向券商校验)")
        state["running"] = False
        return

    headers = {"X-API-Key": BRIDGE_API_KEY} if BRIDGE_API_KEY else {}
    state.update(current={}, symbols=[it["symbol"] for it in items],
                 bars_written=0, done=[], errors=[])
    # 轮询分摊: worker i 负责 items[i::n]
    assignments = [(h, items[i::len(hosts)]) for i, h in enumerate(hosts)]
    logger.info("sync across %d workers: %s", len(hosts),
                {h["name"]: [it["symbol"] for it in its] for h, its in assignments})

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        await asyncio.gather(*(
            _worker_sync(pool, client, h, its)
            for h, its in assignments if its
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
    """每 30s 轮询启用 worker 的 /health, 维护三态状态机 + 事件记录:
      ONLINE   = /health healthy (bridge + MT5 + 账户全就绪)
      DEGRADED = bridge 可达但 MT5 未就绪 (未连接/账户未登录) — 可远程下发账户, 不是离线
      OFFLINE  = bridge 不可达超过 90s 宽限 (避免单次超时抖动)"""
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                hosts = await pool.fetch(
                    "SELECT id, name, host, port, status, runner FROM mt5_hosts WHERE enabled")
                for h in hosts:
                    try:
                        await _beat_one(pool, client, h)
                    except Exception as e:  # 单台异常隔离: 不能冻结其他主机的状态更新
                        logger.warning("heartbeat %s error: %s", h["name"], e)
            except Exception as e:
                logger.warning("heartbeat loop error: %s", e)
            await asyncio.sleep(30)


TRADES_WINDOW_DAYS = 7  # 逐笔重拉的重叠窗口(去重靠主键, 多拉无害; 覆盖迟到平仓)


async def _persist_trades(pool: asyncpg.Pool, client: httpx.AsyncClient, h, account: int) -> None:
    """拉 bridge /trades → 按 position_id 把 deal 腿配对成回合 → upsert trades(增量去重)。
    只落: 已平仓(有 in+out 腿) 且 magic 归属策略(100000+id) 的回合; 持仓中/手动单不入。"""
    headers = {"X-API-Key": BRIDGE_API_KEY} if BRIDGE_API_KEY else {}
    r = await client.get(f"http://{h['host']}:{h['port']}/trades",
                         params={"days": TRADES_WINDOW_DAYS}, headers=headers)
    if r.status_code != 200:
        return
    by_pos: dict = {}
    for d in r.json().get("deals", []):
        by_pos.setdefault(d["position_id"], []).append(d)
    env = h["runner"].upper()
    points = {row["symbol"]: row["point"]
              for row in await pool.fetch("SELECT symbol, point FROM symbols")}
    rows = []
    for pos_id, legs in by_pos.items():
        ins = next((d for d in legs if d["entry"] == "in"), None)
        out = next((d for d in legs if d["entry"] == "out"), None)
        if not ins or not out:                        # 未平仓 → 平仓后下次心跳再纳入
            continue
        magic = ins["magic"]
        if not (100_000 <= magic < 200_000):          # 只落策略回合(手动0/测试999999跳过)
            continue
        if ins["type"] not in ("buy", "sell"):         # 跳过 balance 等非交易腿
            continue
        symbol = ins["symbol"] or out["symbol"]
        pt = points.get(symbol) or 0
        move = (out["price"] - ins["price"]) if ins["type"] == "buy" else (ins["price"] - out["price"])
        rows.append((
            account, pos_id, magic - 100_000, magic, env, symbol, ins["type"], ins["volume"],
            datetime.fromtimestamp(ins["time"], tz=timezone.utc), ins["price"],
            datetime.fromtimestamp(out["time"], tz=timezone.utc), out["price"],
            out["reason"], out["profit"],
            (ins.get("commission") or 0) + (out.get("commission") or 0), out.get("swap") or 0,
            round(move / pt, 1) if pt else None))
    if not rows:
        return
    await pool.executemany(
        "INSERT INTO trades (account, position_id, strategy_id, magic, env, symbol,"
        "   direction, volume, entry_time, entry_price, exit_time, exit_price,"
        "   close_reason, profit, commission, swap, net_points)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)"
        " ON CONFLICT (account, position_id) DO NOTHING",   # 已平仓回合不可变, 去重即可
        rows)


async def _beat_one(pool: asyncpg.Pool, client: httpx.AsyncClient, h) -> None:
    """单台主机的一次心跳探测与状态落库"""
    health = None
    try:
        r = await client.get(f"http://{h['host']}:{h['port']}/health")
        if r.status_code == 200:
            health = r.json()               # 完整 /health JSON, 存库供 web 展示
    except (httpx.HTTPError, ValueError):
        health = None

    if health is not None:  # bridge 可达
        new = "ONLINE" if health.get("status") == "healthy" else "DEGRADED"
        # $2 加 ::text: 同一参数既赋值 varchar 列又与 text 比较, 不显式转换
        # Postgres 会报 "inconsistent types deduced for parameter"
        await pool.execute(
            "UPDATE mt5_hosts SET status=$2::text, last_heartbeat=now(), last_health=$3,"
            " online_at = CASE WHEN $2::text='ONLINE' AND status <> 'ONLINE'"
            "             THEN now() ELSE online_at END"
            " WHERE id=$1", h["id"], new, health)
        if h["status"] != new:
            await log_host_event(pool, h["id"], new)
            logger.info("worker %s %s", h["name"], new)
        # 铁律"不同 worker 不得共用 MT5 账户"由数据库唯一索引执法 (schema/002):
        # 把实际登录账户同步进列, 写失败 = 撞号 (典型: 克隆机自带旧账户), 只告警不中断
        if health.get("login"):
            try:
                await pool.execute(
                    "UPDATE mt5_hosts SET mt5_login=$2, mt5_server=$3 WHERE id=$1"
                    " AND (mt5_login IS DISTINCT FROM $2 OR mt5_server IS DISTINCT FROM $3)",
                    h["id"], health["login"], health.get("server"))
            except asyncpg.UniqueViolationError:
                logger.warning("worker %s 登录的 MT5 账户 %s 已被其他启用 worker 占用 — "
                               "违反铁律, 请换账户", h["name"], health["login"])
        # 每策略战绩快照入库 (strategy_stats): 回测/demo/live 三方对比的数据基础。
        # 按主机角色写对应环境; 策略晋级后旧环境的最后快照保留 — demo vs live 才有对比对象。
        # 只存聚合(近90天窗口), 逐笔回写是 P2
        rn = health.get("runner") or {}
        if h["runner"] and rn.get("per_strategy"):
            await pool.executemany(
                "INSERT INTO strategy_stats (strategy_id, env, trades, wins, profit)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (strategy_id, env) DO UPDATE SET"
                "   trades = EXCLUDED.trades, wins = EXCLUDED.wins,"
                "   profit = EXCLUDED.profit, updated_at = now()",
                [(s["id"], h["runner"].upper(), s["closed"]["trades"],
                  s["closed"]["wins"], s["closed"]["profit"])
                 for s in rn["per_strategy"] if s.get("closed")])
        # 逐笔回合入库(关2对账源数据, v1.6): 拉 /trades → 按 position_id 配对回合 → upsert。
        # 独立 try: 逐笔落库失败不能拖垮心跳状态机(它只是对账用, 不影响 worker 存活判定)
        if h["runner"] and health.get("login"):
            try:
                await _persist_trades(pool, client, h, int(health["login"]))
            except Exception as e:
                logger.warning("persist trades %s failed: %s", h["name"], e)
    else:  # 探测失败: 超过90s宽限才判下线
        row = await pool.fetchrow(
            "UPDATE mt5_hosts SET status='OFFLINE', offline_at=now()"
            " WHERE id=$1 AND status <> 'OFFLINE'"
            "   AND (last_heartbeat IS NULL OR"
            "        last_heartbeat < now() - interval '90 seconds')"
            " RETURNING id", h["id"])
        if row:
            await log_host_event(pool, h["id"], "OFFLINE")
            logger.warning("worker %s OFFLINE", h["name"])
