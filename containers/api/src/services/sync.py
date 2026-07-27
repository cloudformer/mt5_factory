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


async def insert_bars(conn: asyncpg.Connection, symbol: str, bars: list) -> int:
    """K线幂等入库(主键 ON CONFLICT DO NOTHING): 旧编排拉取 与 新 jobs 上传 共用同一落库。"""
    return await _insert_bars(conn, symbol, bars)


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


# ---------- 下载编排反转(v7.2 #3, 2026-07-26 与 Frank 定): jobs 模式 ----------
# api 只把任务写在"桌上"(jobs 表, kind=download), download worker 轮询领取 →
# 自拉 MT5 → 分批 POST /download/bars 回来入库(同一个幂等 upsert)。
# 旧编排(run_full_sync, api 反向拉)保留作兼容路: 没有会领任务的新 worker 时才走。
DOWNLOAD_KIND = "download"
DOWNLOAD_MAX_ATTEMPTS = 5     # 同一任务被收回重派的上限, 超过 = FAILED(防死循环重试)
DOWNLOAD_IDLE_MINUTES = 10    # RUNNING 且 N 分钟没有任何上传动作 = 怠工, 收回重派


async def submit_download_jobs(pool: asyncpg.Pool) -> dict:
    """每个下载品种一条 job(from=库内断点, to=提交时刻), 先清后插(与回测批同款)。"""
    items = await load_download_symbols(pool)
    if not items:
        return {"jobs": 0, "error": "没有开启下载的品种 — 在下载页登记品种(会向券商校验)"}
    now = datetime.now(timezone.utc)
    rows = []
    for it in items:
        last = await pool.fetchval(
            "SELECT max(time) FROM historical_bars WHERE symbol=$1 AND timeframe='M1'",
            it["symbol"])
        rows.append((DOWNLOAD_KIND, {"symbol": it["symbol"], "written": 0,
                                     "from": (last or it["data_start"]).isoformat(),
                                     "to": now.isoformat()}))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM jobs WHERE kind=$1", DOWNLOAD_KIND)
            await conn.executemany("INSERT INTO jobs (kind, payload) VALUES ($1, $2)", rows)
    logger.info("download jobs submitted: %d symbols", len(rows))
    return {"jobs": len(rows), "symbols": [r[1]["symbol"] for r in rows]}


async def claim_download_job(pool: asyncpg.Pool, worker: str):
    """领任务: SKIP LOCKED 抢单(多 download 机并发安全, 铁律6)。
    顺手回收怠工单(10分钟无上传 → 重派; 重派超5次 → FAILED 记明原因)。"""
    await pool.execute(
        "UPDATE jobs SET status='FAILED', finished_at=now(),"
        "       error=concat_ws(' | ', error, '重派超'||$2||'次仍未完成, 放弃(看 worker 日志找上传失败原因)')"
        " WHERE kind=$1 AND status='RUNNING' AND attempts >= $2"
        "   AND started_at < now() - make_interval(mins => $3)",
        DOWNLOAD_KIND, DOWNLOAD_MAX_ATTEMPTS, DOWNLOAD_IDLE_MINUTES)
    await pool.execute(
        "UPDATE jobs SET status='PENDING', worker=NULL, attempts=attempts+1,"
        "       error=concat_ws(' | ', error, '怠工收回(10分钟无上传, 原worker='||coalesce(worker,'?')||')')"
        " WHERE kind=$1 AND status='RUNNING'"
        "   AND started_at < now() - make_interval(mins => $2)",
        DOWNLOAD_KIND, DOWNLOAD_IDLE_MINUTES)
    return await pool.fetchrow(
        "UPDATE jobs SET status='RUNNING', worker=$2, started_at=now()"
        " WHERE id = (SELECT id FROM jobs WHERE kind=$1 AND status='PENDING'"
        "             ORDER BY (payload->>'symbol'), id LIMIT 1 FOR UPDATE SKIP LOCKED)"
        " RETURNING id, payload", DOWNLOAD_KIND, worker)


async def download_progress(pool: asyncpg.Pool):
    """jobs 模式进度, 拼成与旧内存 state 同形 → web 下载页零改动。无下载 jobs = None(用旧 state)。"""
    rows = await pool.fetch(
        "SELECT status, worker, error, payload FROM jobs WHERE kind=$1", DOWNLOAD_KIND)
    if not rows:
        return None
    out = {"running": False, "current": {}, "symbols": [], "bars_written": 0,
           "done": [], "errors": [], "mode": "jobs"}
    for r in rows:
        p = r["payload"]
        sym = p.get("symbol", "?")
        out["symbols"].append(sym)
        out["bars_written"] += int(p.get("written") or 0)
        if r["status"] == "DONE":
            out["done"].append(sym)
        elif r["status"] == "FAILED":
            out["errors"].append(f"{sym}@{r['worker'] or '?'}: {r['error'] or '未知原因'}")
        else:  # PENDING / RUNNING
            out["running"] = True
            if r["status"] == "RUNNING":
                out["current"][r["worker"] or "?"] = sym
    return out


async def log_host_event(pool: asyncpg.Pool, host_id: int, event: str, detail: dict | None = None):
    """worker 生命周期事件入库 (追踪用)"""
    await pool.execute(
        "INSERT INTO mt5_host_events (host_id, event, detail) VALUES ($1, $2, $3)",
        host_id, event, detail or {})


async def _touch_runtime(pool: asyncpg.Pool, ids: list[int], host: str):
    """运行区间批量推进(strategy_runtime, schema/019): ids = worker 心跳里真实加载中的策略。
    一条语句完成三分支, 写库次数与策略数量无关(每 worker 每写入间隔 2 条):
      最近段 run_to 距今 < 写入间隔      → 跳过(节流, 心跳30s没必要每跳写)
      距今 ∈ [写入间隔, 裂段阈值)        → 推进该段 run_to = now()
      距今 ≥ 裂段阈值 / 无任何段          → 新开一段(run_from = run_to = now())
    死机/下架 = 心跳停 = run_to 自动定格, 无需任何"关闭"动作。"""
    if not ids:
        return
    cfg = {r["key"]: r["value"] for r in await pool.fetch(
        "SELECT key, value FROM config WHERE key IN ('runtime_write_minutes',"
        " 'runtime_gap_minutes')")}
    write_min = int(cfg.get("runtime_write_minutes") or 5)
    gap_min = max(int(cfg.get("runtime_gap_minutes") or 15), write_min + 1)  # 阈值必须>间隔
    await pool.execute(
        """
        WITH latest AS (            -- 每个策略最近的一段
          SELECT DISTINCT ON (strategy_id) strategy_id, run_from, run_to
            FROM strategy_runtime WHERE strategy_id = ANY($1::int[])
           ORDER BY strategy_id, run_from DESC
        ), bumped AS (              -- 未裂段且到了写入间隔 → 推进
          UPDATE strategy_runtime r SET run_to = now(), host = $2
            FROM latest l
           WHERE r.strategy_id = l.strategy_id AND r.run_from = l.run_from
             AND l.run_to >  now() - make_interval(mins => $4)
             AND l.run_to <= now() - make_interval(mins => $3)
        )                           -- 无段 / 已裂段 → 新开一段
        INSERT INTO strategy_runtime (strategy_id, run_from, run_to, host)
        SELECT sid, now(), now(), $2 FROM unnest($1::int[]) AS sid
         WHERE NOT EXISTS (SELECT 1 FROM latest l WHERE l.strategy_id = sid
                             AND l.run_to > now() - make_interval(mins => $4))
        """,
        ids, host, write_min, gap_min)


HEARTBEAT_LEADER_LOCK = 714002  # advisory lock key: 心跳循环选主(铁律6, 多副本只跑一份)


async def heartbeat_loop(pool: asyncpg.Pool):
    """每 30s 轮询启用 worker 的 /health, 维护三态状态机 + 事件记录:
      ONLINE   = /health healthy (bridge + MT5 + 账户全就绪)
      DEGRADED = bridge 可达但 MT5 未就绪 (未连接/账户未登录) — 可远程下发账户, 不是离线
      OFFLINE  = bridge 不可达超过 90s 宽限 (避免单次超时抖动)

    选主(铁律6): 拿到 advisory lock 的副本才轮询(锁挂在专用连接上, 持有到进程死);
    其余副本待机每 30s 重试 — 主挂了连接断开锁自动释放, 待机者接管, 无需任何协调服务。"""
    while True:
        try:
            async with pool.acquire() as lock_conn:
                if not await lock_conn.fetchval(
                        "SELECT pg_try_advisory_lock($1)", HEARTBEAT_LEADER_LOCK):
                    await asyncio.sleep(30)   # 别的副本是主, 待机重试(释放连接)
                    continue
                logger.info("heartbeat leader acquired (lock %s)", HEARTBEAT_LEADER_LOCK)
                # 当主: lock_conn 挂着锁不还池(池 min2/max10, 占1条无碍), 循环体照旧用池
                async with httpx.AsyncClient(timeout=5) as client:
                    while True:
                        try:
                            hosts = await pool.fetch(
                                "SELECT id, name, host, port, status, runner"
                                " FROM mt5_hosts WHERE enabled")
                            for h in hosts:
                                try:
                                    await _beat_one(pool, client, h)
                                except Exception as e:  # 单台异常隔离
                                    logger.warning("heartbeat %s error: %s", h["name"], e)
                        except asyncpg.PostgresConnectionError:
                            raise   # 池级连接故障 → 掉出主循环重新选主
                        except Exception as e:
                            logger.warning("heartbeat loop error: %s", e)
                        if lock_conn.is_closed():   # 锁连接断 = 主身份已失效, 停止双写
                            raise asyncpg.PostgresConnectionError("leader lock conn lost")
                        await asyncio.sleep(30)
        except Exception as e:
            logger.warning("heartbeat leader error (re-electing): %s", e)
            await asyncio.sleep(10)


TRADES_BACKFILL_DAYS = 90   # 首次/空库回填窗口(足够大, 保证历史完整)
TRADES_OVERLAP_DAYS = 3     # 稳态重叠(补迟到平仓; 去重靠主键, 多拉无害)


async def trades_window_days(pool: asyncpg.Pool, account: int) -> int:
    """成交窗口自适应(推/拉共用): 空库回填 BACKFILL 天; 稳态 = 最新平仓缺口 + OVERLAP 天。
    推送模式下 api 把这个数放进心跳应答, worker 下一拍按它收集 — 窗口智慧留在库侧。"""
    last = await pool.fetchval("SELECT max(exit_time) FROM trades WHERE account=$1", account)
    if last is None:
        return TRADES_BACKFILL_DAYS
    gap = (datetime.now(timezone.utc) - last).days
    return min(TRADES_BACKFILL_DAYS, max(TRADES_OVERLAP_DAYS, gap + TRADES_OVERLAP_DAYS))


async def _persist_trades(pool: asyncpg.Pool, client: httpx.AsyncClient, h, account: int,
                          broker: str = None) -> None:
    """拉 bridge /trades → ingest_trades 落库(v7.2 双栈的拉侧; 推侧见 hosts.push_heartbeat)。"""
    days = await trades_window_days(pool, account)
    headers = {"X-API-Key": BRIDGE_API_KEY} if BRIDGE_API_KEY else {}
    r = await client.get(f"http://{h['host']}:{h['port']}/trades",
                         params={"days": days}, headers=headers)
    if r.status_code != 200:   # 非200 = bridge 侧明确拒绝/异常, 带原因记日志, 下一拍重试
        logger.warning("pull trades %s(acct %s) failed: HTTP %s %s",
                       h["name"], account, r.status_code, r.text[:120])
        return
    await ingest_trades(pool, h, account, r.json().get("deals", []), broker)


async def ingest_trades(pool: asyncpg.Pool, h, account: int, deals: list,
                        broker: str = None) -> tuple[int, int]:
    """deals(MT5 原样) → 按 position_id 配对回合 → 幂等 upsert。返回 (落库回合数, 坏回合数)。
    推(心跳捎带)/拉(_persist_trades)共用同一解析同一落库 — 并跑零分叉, 重复喂零副作用。
    落【全部】已平仓回合(不按 magic 过滤, 与 MT5 100% 一致): strategy_id=magic-100000(策略区间)否则 NULL;
    "只看策略单"的过滤放到对账/分析时。持仓中不入(铁律: 只存历史)。"""
    by_pos: dict = {}
    for d in deals:
        pid = d.get("position_id")     # D1: 缺 position_id 的畸形 deal 直接跳过, 不进分组
        if pid is not None:
            by_pos.setdefault(pid, []).append(d)
    env = h["runner"].upper()
    points = {row["symbol"]: row["point"]
              for row in await pool.fetch("SELECT symbol, point FROM symbols")}
    rows, bad = [], 0
    for pos_id, legs in by_pos.items():
        try:  # D1: 单个回合坏数据只跳过它, 不让一条脏数据拖垮整批落库
            ins = next((d for d in legs if d.get("entry") == "in"), None)
            out = next((d for d in legs if d.get("entry") == "out"), None)
            if not ins or not out:                     # 未平仓 → 平仓后下次心跳再纳入
                continue
            if ins.get("type") not in ("buy", "sell"):  # 跳过 balance(入金/出金非交易腿)
                continue
            magic = int(ins.get("magic") or 0)
            strategy_id = magic - 100_000 if 100_000 <= magic < 200_000 else None  # 非策略单=NULL, 仍落库
            symbol = ins.get("symbol") or out.get("symbol")
            pt = points.get(symbol) or 0
            move = ((out["price"] - ins["price"]) if ins["type"] == "buy"
                    else (ins["price"] - out["price"]))
            rows.append((
                account, int(pos_id), strategy_id, magic, env, symbol, ins["type"], ins["volume"],
                datetime.fromtimestamp(ins["time"], tz=timezone.utc), ins["price"],
                datetime.fromtimestamp(out["time"], tz=timezone.utc), out["price"],
                out.get("reason"), out.get("profit") or 0,
                (ins.get("commission") or 0) + (out.get("commission") or 0), out.get("swap") or 0,
                round(move / pt, 1) if pt else None, broker))
        except (KeyError, TypeError, ValueError) as e:
            bad += 1
            logger.warning("skip malformed round-trip pos=%s @ %s: %s", pos_id, h["name"], e)
    if bad:
        logger.warning("ingest trades %s: %d 坏回合已跳过(其余照落)", h["name"], bad)
    if not rows:
        return 0, bad
    await pool.executemany(
        "INSERT INTO trades (account, position_id, strategy_id, magic, env, symbol,"
        "   direction, volume, entry_time, entry_price, exit_time, exit_price,"
        "   close_reason, profit, commission, swap, net_points, broker)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)"
        # 回合不可变; 仅回填/纠正 broker(新加列, 老行为NULL) — 其余字段不动
        " ON CONFLICT (account, position_id) DO UPDATE SET broker = EXCLUDED.broker"
        "   WHERE trades.broker IS DISTINCT FROM EXCLUDED.broker",
        rows)
    # D2: 一致性自检 — 落库后核对本次 MT5 给的回合是否都进了库; 缺了=疑似漏存 → 告警 + 主机事件
    stored = await pool.fetchval(
        "SELECT count(*) FROM trades WHERE account=$1 AND position_id = ANY($2)",
        account, [row[1] for row in rows])
    if stored < len(rows):
        logger.warning("trades consistency %s: MT5 给 %d 回合, DB 仅 %d — 疑似漏存",
                       h["name"], len(rows), stored)
        await log_host_event(pool, h["id"], "trades_mismatch",
                             {"account": account, "mt5": len(rows), "db": stored})
    return stored, bad


async def ingest_health(pool: asyncpg.Pool, h, health: dict) -> None:
    """心跳收货(推/拉共用一套, v7.2 一期): 状态机 + last_health + 账号认领 + stats
    + runtime + 异常事件。h 需含 id/name/status/runner。
    trades 不在这里 — #2 未迁, 仍由轮询侧 _beat_one 拉取。"""
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
    # v5.0-B1: 主键 (strategy_id, account) — 多挂载后同策略多账户各存各的, 不互相覆盖;
    # env 降为属性列(账户随主机角色变则跟着改)。无 login 的心跳不写: 没有账户维度没法归位
    if h["runner"] and rn.get("per_strategy") and health.get("login"):
        await pool.executemany(
            "INSERT INTO strategy_stats (strategy_id, env, account, trades, wins, profit)"
            " VALUES ($1, $2, $3, $4, $5, $6)"
            " ON CONFLICT (strategy_id, account) DO UPDATE SET"
            "   env = EXCLUDED.env, trades = EXCLUDED.trades, wins = EXCLUDED.wins,"
            "   profit = EXCLUDED.profit, updated_at = now()",
            [(s["id"], h["runner"].upper(), health["login"], s["closed"]["trades"],
              s["closed"]["wins"], s["closed"]["profit"])
             for s in rn["per_strategy"] if s.get("closed")])
    # 运行区间(strategy_runtime, schema/019): 名单里的策略 = 此刻真实在跑 →
    # 批量"推进最近段 / 新开一段"。独立 try: 区间记录失败不拖垮心跳状态机
    if h["runner"] and rn.get("per_strategy"):
        try:
            await _touch_runtime(
                pool, [s["id"] for s in rn["per_strategy"] if s.get("id")], h["name"])
        except Exception as e:
            logger.warning("touch runtime %s failed: %s", h["name"], e)
    # worker 异常事件入库(2026-07-26): runner 状态变化(断报价/下单失败/加载失败)随心跳
    # 捎带(缓冲最近50条, 每条唯一 eid), 唯一索引(schema/044)+ON CONFLICT 幂等 —
    # 重复看到同一批静默跳过。库只存状态变化, 全量决策在 worker 本地 JSONL。
    if rn.get("events"):
        try:
            await pool.executemany(
                "INSERT INTO mt5_host_events (host_id, event, detail) VALUES ($1, $2, $3)"
                " ON CONFLICT (host_id, (detail->>'eid'))"
                " WHERE (detail->>'eid') IS NOT NULL DO NOTHING",
                [(h["id"], str(e.get("kind", "?"))[:16], e)
                 for e in rn["events"] if isinstance(e, dict) and e.get("eid")])
        except Exception as e:
            logger.warning("ingest events %s failed: %s", h["name"], e)


async def _beat_one(pool: asyncpg.Pool, client: httpx.AsyncClient, h) -> None:
    """单台主机的一次心跳轮询: 探测 /health → 收货(ingest_health) → 拉 trades。
    双栈过渡(v7.2 一期): 该机在推心跳(last_health 带 push_v)且 75s 内新鲜 →
    跳过反向探测(推送已收货), 只保留 trades 拉取(#2 未迁);
    推送停了(降级/回滚)超时自动回到轮询 — 自愈, 无需任何开关。"""
    push = await pool.fetchrow(
        "SELECT mt5_login, mt5_server, (last_health ? 'trades_v') AS trades_pushed"
        "  FROM mt5_hosts WHERE id=$1 AND last_health ? 'push_v'"
        "   AND last_heartbeat > now() - interval '75 seconds'", h["id"])
    if push is not None:
        # trades_v = 上一拍推送已捎成交且入库成功(hosts.push_heartbeat 打标) → 拉取全免;
        # 没带成交/入库失败则不打标 → 这里回退拉取, 数据不丢(双栈#2)
        if not push["trades_pushed"] and h["runner"] and push["mt5_login"]:
            try:
                await _persist_trades(pool, client, h, int(push["mt5_login"]), push["mt5_server"])
            except Exception as e:
                logger.warning("persist trades %s failed: %s", h["name"], e)
        return
    health = None
    try:
        r = await client.get(f"http://{h['host']}:{h['port']}/health")
        if r.status_code == 200:
            health = r.json()               # 完整 /health JSON, 存库供 web 展示
    except (httpx.HTTPError, ValueError):
        health = None

    if health is not None:  # bridge 可达
        health.pop("push_v", None)          # 轮询取回的不算推送(防旧标记粘住跳过逻辑)
        await ingest_health(pool, h, health)
        # 逐笔回合入库(关2对账源数据, v1.6): 拉 /trades → 按 position_id 配对回合 → upsert。
        # 独立 try: 逐笔落库失败不能拖垮心跳状态机(它只是对账用, 不影响 worker 存活判定)
        if h["runner"] and health.get("login"):
            try:
                await _persist_trades(pool, client, h, int(health["login"]), health.get("server"))
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
