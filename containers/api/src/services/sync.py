"""数据同步与心跳 — services 层: 下载任务(jobs, worker 领取)、心跳收货与看门狗、host事件。
v7.2 收口(2026-07-26 与 Frank 定): api 对 worker 零出站 — 本模块不再有任何 HTTP 客户端;
数据全部由 worker 推(心跳/成交/K线上传), api 只收货 + 看门狗判离线。"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger("sync")


async def load_download_symbols(pool: asyncpg.Pool) -> list:
    """要下载的品种及其独立起始日期 — 唯一来源 symbols 表 (download=TRUE)。
    返回 [{symbol, data_start(UTC datetime)}]; 每品种自己的起始日期(BTCUSD≠EURUSD)。"""
    rows = await pool.fetch(
        "SELECT symbol, data_start FROM symbols WHERE download ORDER BY symbol")
    return [{"symbol": r["symbol"],
             "data_start": datetime(r["data_start"].year, r["data_start"].month,
                                    r["data_start"].day, tzinfo=timezone.utc)}
            for r in rows]


async def insert_bars(conn: asyncpg.Connection, symbol: str, bars: list,
                      timeframe: str = "M1") -> int:
    """K线幂等入库(主键 ON CONFLICT DO NOTHING): 旧编排拉取 与 新 jobs 上传 共用同一落库。
    timeframe: M1(唯一原始数据) / D1(例外补下, 2026-07-29 定: MetaQuotes M1 仅存~4个月
    而 D1 有16年+, regime 长视野用原生 D1 补头; 回测/对账仍只读 M1, 尺子不换料)"""
    return await _insert_bars(conn, symbol, bars, timeframe)


async def _insert_bars(conn: asyncpg.Connection, symbol: str, bars: list,
                       timeframe: str = "M1") -> int:
    records = [
        (symbol, timeframe, datetime.fromtimestamp(b["time"], tz=timezone.utc),
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


# 旧编排(run_full_sync: api 反向拉 bridge /rates)已删(2026-07-26 v7.2 收口) —
# 下载唯一路径 = 下方 jobs 模式; 老 worker 不再被支持(全员已更新)。

# ---------- 下载编排(v7.2 #3, 2026-07-26 与 Frank 定): jobs 模式 ----------
# api 只把任务写在"桌上"(jobs 表, kind=download), download worker 轮询领取 →
# 自拉 MT5 → 分批 POST /download/bars 回来入库(同一个幂等 upsert)。
# (收口后 jobs 是唯一下载路径)
DOWNLOAD_KIND = "download"
DOWNLOAD_MAX_ATTEMPTS = 5     # 同一任务被收回重派的上限, 超过 = FAILED(防死循环重试)
DOWNLOAD_IDLE_MINUTES = 10    # RUNNING 且 N 分钟没有任何上传动作 = 怠工, 收回重派


async def submit_download_jobs(pool: asyncpg.Pool, only_tfs: list | None = None) -> dict:
    """每个下载品种×每个周期层一条 job(from=库内断点, to=提交时刻), 先清后插(与回测批同款)。
    only_tfs: 本次只下这些层(下载页勾选, 须为配置层的子集); None/空 = 配置的全部层。"""
    items = await load_download_symbols(pool)
    if not items:
        return {"jobs": 0, "error": "没有开启下载的品种 — 在下载页登记品种(会向券商校验)"}
    now = datetime.now(timezone.utc)
    # 下载周期层(2026-07-29 与 Frank 定, 配置唯一源=数据库 schema/049 种子, 配置页可见可改):
    # 默认 ["M1","D1"] — M1=唯一原始数据(回测/对账/聚合的原料); D1=例外补下(MetaQuotes
    # M1 仅存~4个月而 D1 有16年+, regime 长视野靠原生 D1 补头; 行量可忽略, 20年≈5200行)。
    # 两层共用 data_start, 各取所能: M1 空跑过没有数据的年份, D1 吃满。
    # 中间层(H1/M15)默认不下 — 回测尺子不换料; 要下去配置页勾选。
    tfs = await pool.fetchval("SELECT value FROM config WHERE key='download_timeframes'")
    if not tfs:   # 配置缺失 = schema 种子没跑到, 如实报错(兜底默认值不落代码)
        return {"jobs": 0, "error": "config download_timeframes 缺失 — 重启 api 让 schema/049 种子落库"}
    if only_tfs:   # 下载页本次勾选: 只允许配置层的子集(选项由配置生成, 越界如实拒)
        bad = [t for t in only_tfs if t not in tfs]
        if bad:
            return {"jobs": 0, "error": f"周期 {bad} 不在配置的下载层 {tfs} 里 — 先去配置页勾选"}
        tfs = [t for t in tfs if t in only_tfs]
        if not tfs:
            return {"jobs": 0, "error": "至少勾选一个周期层"}
    rows = []
    for it in items:
        for tf in tfs:
            span = await pool.fetchrow(
                "SELECT min(time) AS first, max(time) AS last"
                "  FROM historical_bars WHERE symbol=$1 AND timeframe=$2",
                it["symbol"], tf)
            # 起点三分法(2026-07-28 修头部缺口盲区): 空库=data_start; 头部有缺口(最早一根晚于
            # data_start 超7天容差, 周末/假日不算) = 回到 data_start 整段重下到现在 —
            # 改 data_start 往前挖历史靠这条生效; 分段深挖(先2020再2018)会重拉已有段, 幂等无害,
            # 宁可白跑不可漏段(2026-07-28 Frank 定); 头部完整 = 从最新断点续传。
            head_gap = (span["first"] is not None
                        and span["first"] > it["data_start"] + timedelta(days=7))
            frm = it["data_start"] if (span["first"] is None or head_gap) else span["last"]
            rows.append((DOWNLOAD_KIND, {"symbol": it["symbol"], "timeframe": tf,
                                         "written": 0, "from": frm.isoformat(),
                                         "to": now.isoformat()}))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM jobs WHERE kind=$1", DOWNLOAD_KIND)
            await conn.executemany("INSERT INTO jobs (kind, payload) VALUES ($1, $2)", rows)
    logger.info("download jobs submitted: %d symbols", len(rows))
    return {"jobs": len(rows), "symbols": [r[1]["symbol"] for r in rows]}


async def claim_download_job(pool: asyncpg.Pool, worker: str, server: str | None,
                             tf_capable: bool = False):
    """领任务: SKIP LOCKED 抢单(多 download 机并发安全, 铁律6), 快者多得但**必须券商匹配**
    (纪律: 数据从实际交易的券商服务器下载) — 品种 broker == worker 登录 server 才可领;
    品种无券商标注(老行) = 谁都可领; worker 未上报账户(server=None) = 只能领无标注的。
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
        " WHERE id = (SELECT j.id FROM jobs j"
        "             LEFT JOIN symbols s ON s.symbol = j.payload->>'symbol'"
        "             WHERE j.kind=$1 AND j.status='PENDING'"
        "               AND (s.broker IS NULL OR s.broker = $3)"   # 券商匹配
        # 防污染保险①: 非 M1 任务只给带 dl_tf 能力标记的 worker(老 worker 拉不了高周期)
        "               AND (COALESCE(j.payload->>'timeframe', 'M1') = 'M1' OR $4)"
        # FOR UPDATE OF j: 只锁 jobs 行 — 裸 FOR UPDATE 会碰外连接可空侧(symbols)直接报错
        "             ORDER BY (j.payload->>'symbol'), j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED)"
        " RETURNING id, payload", DOWNLOAD_KIND, worker, server, tf_capable)


async def download_progress(pool: asyncpg.Pool):
    """jobs 模式进度, 拼成与旧内存 state 同形 → web 下载页零改动。无下载 jobs = None(用旧 state)。"""
    rows = await pool.fetch(
        "SELECT status, worker, error, payload, finished_at FROM jobs WHERE kind=$1",
        DOWNLOAD_KIND)
    if not rows:
        return None
    # 上次同步完成(Last Sync): 本批全部结束时间(最后一个任务的 finished_at); 有任务还在跑=None
    fins = [r["finished_at"] for r in rows]
    last_sync = max(fins).isoformat() if all(fins) else None
    out = {"running": False, "current": {}, "symbols": [], "bars_written": 0,
           "done": [], "errors": [], "mode": "jobs", "last_sync": last_sync}
    for r in rows:
        p = r["payload"]
        sym = p.get("symbol", "?")
        if p.get("timeframe", "M1") != "M1":   # D1 补头任务在进度里带周期后缀, 与 M1 区分
            sym = f"{sym}·{p['timeframe']}"
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


async def _auto_sync_tick(pool: asyncpg.Pool) -> None:
    """自动增量同步(2026-08-01 与 Frank 定, 心跳主节点搭车): 距上次 ≥ auto_sync_hours
    小时就自动投一批增量下载(按配置周期层, 断点续传幂等) — 把"每天人肉点同步"自动化,
    regime 当日格的原料保鲜靠它。规则:
    - config auto_sync_hours(schema/055 种子 6, 配置页只读, 0=关闭);
    - 有下载批次在跑(手动/上一班未完) → 本拍跳过不清人家的单, 30s 后再看;
    - 上次时刻记 config auto_sync_last(UPSERT 一行, 无新表); 首次部署缺失 = 立即补一班
      (增量便宜; 全新空库则等价于自动开始首轮全量, 本来也要下)。"""
    hours = await pool.fetchval("SELECT value FROM config WHERE key='auto_sync_hours'")
    if not hours or int(hours) <= 0:
        return
    now = datetime.now(timezone.utc)
    last = await pool.fetchval("SELECT value FROM config WHERE key='auto_sync_last'")
    if last and datetime.fromisoformat(last) > now - timedelta(hours=int(hours)):
        return
    active = await pool.fetchval(
        "SELECT count(*) FROM jobs WHERE kind=$1 AND status IN ('PENDING','RUNNING')",
        DOWNLOAD_KIND)
    if active:
        return   # 避让: submit 是先清后插, 不能打断在跑的批
    r = await submit_download_jobs(pool)
    await pool.execute(
        "INSERT INTO config (key, value) VALUES ('auto_sync_last', $1)"
        " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
        now.isoformat())
    logger.info("auto sync submitted: %s jobs (every %sh)", r.get("jobs"), hours)


async def heartbeat_loop(pool: asyncpg.Pool):
    """worker 在线看门狗(v7.2 收口后: api 零出站, 不再探测任何 worker)。
    worker 每 30s 主动推心跳(hosts.push_heartbeat 收货并置 ONLINE/DEGRADED);
    这里只做反向裁定: last_heartbeat 超 90s 宽限(3 拍没到) → OFFLINE + 事件。

    选主(铁律6): 拿到 advisory lock 的副本才裁定(锁挂在专用连接上, 持有到进程死);
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
                while True:
                    try:
                        rows = await pool.fetch(
                            "UPDATE mt5_hosts SET status='OFFLINE', offline_at=now()"
                            " WHERE enabled AND status <> 'OFFLINE'"
                            "   AND (last_heartbeat IS NULL OR"
                            "        last_heartbeat < now() - interval '90 seconds')"
                            " RETURNING id, name")
                        for r in rows:
                            await log_host_event(pool, r["id"], "OFFLINE")
                            logger.warning("worker %s OFFLINE (90s 无心跳推送)", r["name"])
                    except asyncpg.PostgresConnectionError:
                        raise   # 池级连接故障 → 掉出主循环重新选主
                    except Exception as e:
                        logger.warning("heartbeat watchdog error: %s", e)
                    try:   # 自动增量同步搭车(只有主节点跑到这里, 多副本天然不重复投)
                        await _auto_sync_tick(pool)
                    except Exception as e:
                        logger.warning("auto sync tick error: %s", e)
                    if lock_conn.is_closed():   # 锁连接断 = 主身份已失效, 停止双写
                        raise asyncpg.PostgresConnectionError("leader lock conn lost")
                    await asyncio.sleep(30)
        except Exception as e:
            logger.warning("heartbeat leader error (re-electing): %s", e)
            await asyncio.sleep(10)


TRADES_BACKFILL_DAYS = 90   # 首次/空库回填窗口(足够大, 保证历史完整)
TRADES_OVERLAP_DAYS = 3     # 稳态重叠(补迟到平仓; 去重靠主键, 多拉无害)


async def trades_window_days(pool: asyncpg.Pool, account: int) -> int:
    """成交窗口自适应: 空库回填 BACKFILL 天; 稳态 = 最新平仓缺口 + OVERLAP 天。
    api 把这个数放进心跳应答, worker 下一拍按它收集 — 窗口智慧留在库侧。
    (拉取路 _persist_trades 已删, 2026-07-26 v7.2 收口: 成交唯一入口 = 心跳捎带)"""
    last = await pool.fetchval("SELECT max(exit_time) FROM trades WHERE account=$1", account)
    if last is None:
        return TRADES_BACKFILL_DAYS
    gap = (datetime.now(timezone.utc) - last).days
    return min(TRADES_BACKFILL_DAYS, max(TRADES_OVERLAP_DAYS, gap + TRADES_OVERLAP_DAYS))


async def ingest_trades(pool: asyncpg.Pool, h, account: int, deals: list,
                        broker: str | None = None) -> tuple[int, int]:
    """deals(MT5 原样) → 按 position_id 配对回合 → 幂等 upsert。返回 (落库回合数, 坏回合数)。
    唯一入口 = 心跳捎带(hosts.push_heartbeat, 收口后拉取路已删); 重复喂零副作用(幂等)。
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


# _beat_one(反向探测 /health + 拉 /trades 的双栈轮询)已删(2026-07-26 v7.2 收口):
# 心跳/成交唯一入口 = worker 推送(hosts.push_heartbeat → ingest_health/ingest_trades);
# 在线判定 = heartbeat_loop 看门狗(没收到=下线)。api 对 worker 零出站。
