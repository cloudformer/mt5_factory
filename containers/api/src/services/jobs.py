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

from src.services import backtest, regime

logger = logging.getLogger("jobs")

# 工种(2026-08-05 与 Frank 定, 命名跟随各自概念既有的名字, 不新造词):
#   backtest       批量回测(回测页)
#   regime_screen  自动化筛选v1(现跑回测 + regime 切片判定)
#   oos_v2         自动化筛选-oos_v2(现跑 20 年回测 + 三期六段判定, v0.6)
# 各工种"一次一批"独立自清理(submit 只删同工种旧批) — 筛选不会抹掉手上的批量回测, 反之亦然;
# 同时跑只是共享 worker 排队(总量守恒)。下载队列在 sync.py(DOWNLOAD_KIND), 消费在 worker 侧
KIND = "backtest"            # 默认工种(不传 kind 的老调用 = 批量回测, 行为不变)
SCREEN_KIND = "regime_screen"
OOS_KIND = "oos_v2"
OOS_JUDGE_KIND = "oos_v2_judge"        # oos_v2 判定任务(判定下放 worker, 按块并行)
SCREEN_JUDGE_KIND = "regime_screen_judge"   # v1 判定任务(2026-08-08 Frank 定: 所有回测统一下放)
MAP_KIND = "regime_map"      # 策略×regime 映射规律(2026-08-11): 纯计算不跑引擎, 复用回测行
ENGINE_KINDS = (KIND, OOS_KIND, SCREEN_KIND)   # "跑一次回测"的工种, 同一执行路径(_run_one)
CLAIM_KINDS = ENGINE_KINDS + (OOS_JUDGE_KIND, SCREEN_JUDGE_KIND, MAP_KIND)  # 抢单认的全部工种
POLL_SECONDS = 3             # 队列空时的轮询间隔
LEASE_MINUTES = 30           # RUNNING 超时视为消费者死单, 扫回重试(单个回测秒级, 30分钟很宽)
MAX_ATTEMPTS = 2             # 含首跑; 超过则 FAILED(错误留在行里可查)
WORKER = f"{socket.gethostname()}:{os.getpid()}"


async def submit_batch(pool: asyncpg.Pool, items: list[dict], kind: str = KIND) -> int:
    """新批次投递: 删光【同工种】旧批次(自清理, 铁律3 — 每工种只留最新一批) + 整批插入。
    并行批次由调用方(routes)先查 has_active 拒绝, 这里不重复把关。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM jobs WHERE kind = $1", kind)
            await conn.executemany(
                "INSERT INTO jobs (kind, payload) VALUES ($1, $2)",
                [(kind, it) for it in items])
    return len(items)


async def has_active(pool: asyncpg.Pool, kind: str = KIND) -> bool:
    return await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM jobs WHERE kind=$1"
        " AND status IN ('PENDING','RUNNING'))", kind)


async def progress(pool: asyncpg.Pool, kind: str = KIND) -> dict:
    """进度聚合(与旧 bt_state 同结构, web 零改动):
    {running, current, done, total, errors, last_finished}"""
    rows = await pool.fetch(
        "SELECT status, count(*) AS n FROM jobs WHERE kind=$1 GROUP BY status", kind)
    n = {r["status"]: r["n"] for r in rows}
    total = sum(n.values())
    done = n.get("DONE", 0) + n.get("FAILED", 0)
    current = None
    if n.get("RUNNING"):
        p = await pool.fetchval(
            "SELECT payload FROM jobs WHERE kind=$1 AND status='RUNNING'"
            " ORDER BY started_at DESC LIMIT 1", kind)
        if p:
            current = f"{p.get('name', p.get('strategy_id'))} @ {p.get('symbol')}"
    errors = [f"{r['payload'].get('name')} @ {r['payload'].get('symbol')}: {r['error']}"
              for r in await pool.fetch(
                  "SELECT payload, error FROM jobs WHERE kind=$1 AND status='FAILED'"
                  " ORDER BY id LIMIT 50", kind)]
    # 本批最后一个任务结束的时刻(2026-08-05 Frank 要: 概览任务表显示时间) —
    # 跑着的批次也给已完成部分的最新时刻, 空批次 None
    last = await pool.fetchval(
        "SELECT max(finished_at) FROM jobs WHERE kind=$1 AND finished_at IS NOT NULL", kind)
    return {"running": (n.get("PENDING", 0) + n.get("RUNNING", 0)) > 0,
            "current": current, "done": done, "total": total, "errors": errors,
            "last_finished": last.isoformat() if last else None}


async def _reclaim(pool: asyncpg.Pool):
    """租约回收: RUNNING 超时的死单 → 未超次数扫回 PENDING 重试, 超了标 FAILED"""
    n = await pool.execute(
        "UPDATE jobs SET"
        "   status = CASE WHEN attempts >= $2 THEN 'FAILED' ELSE 'PENDING' END,"
        "   error  = CASE WHEN attempts >= $2"
        "            THEN coalesce(error || ' | ', '') || 'lease expired' ELSE error END,"
        "   finished_at = CASE WHEN attempts >= $2 THEN now() ELSE NULL END"
        " WHERE kind = ANY($3) AND status='RUNNING'"
        "   AND started_at < now() - make_interval(mins => $1)",
        LEASE_MINUTES, MAX_ATTEMPTS, list(CLAIM_KINDS))
    if n != "UPDATE 0":
        logger.warning("reclaimed stale jobs: %s", n)


async def _run_one(pool: asyncpg.Pool, payload: dict, cache: dict):
    """执行一个 backtest job(策略×品种)。策略/品种/配置临跑现查(最新);
    M1 按 (品种,时间窗) 缓存在消费者内存 — 抢单按品种排序, 同品种连续命中。"""
    sym = payload["symbol"]
    t_from = datetime.fromisoformat(payload["from"])
    t_to = datetime.fromisoformat(payload["to"])
    s = await pool.fetchrow(
        "SELECT id, name, template, params, timeframe, metadata, broker"
        "  FROM strategies WHERE id=$1",
        payload["strategy_id"])
    if s is None:
        raise ValueError("strategy deleted")
    # v2.3 户口制: 数据世界跟策略户口走(跨品种验证行也用同户口券商的数据 — 对比三铁律)
    meta = await pool.fetchrow(
        "SELECT point, broker FROM symbols WHERE symbol=$1 AND broker=$2",
        sym, s["broker"])
    if meta is None:
        raise ValueError(f"symbol {sym}@{s['broker']} not in symbols table")
    # 复用守卫(2026-08-07 全局统一): 有效期内已有覆盖本窗的行 → 秒完不进引擎。
    # payload.reuse_days = 单ID点名把页面「有效期」随任务带来(默认1天档, 填0=本次实际跑);
    # 没带 = 批量/筛选档, 用全局配置。轻量版 reuse_ok 只判存在不搬 trades(2026-08-08)
    if await backtest.reuse_ok(pool, s["id"], sym, t_from, t_to,
                               days=payload.get("reuse_days"), broker=s["broker"]):
        return
    key = (sym, s["broker"], payload["from"], payload["to"])
    if cache.get("key") != key:
        cache["m1"] = await backtest.load_m1(pool, sym, t_from, t_to, s["broker"])
        cache["key"] = key
    if cache["m1"] is None:
        raise ValueError(f"no M1 data for {sym}, run /syncdata first")
    oos_split = await pool.fetchval(
        "SELECT value FROM config WHERE key='backtest_oos_split'") or 0.7
    # 移动止损(v0.9)回落: 策略 params 没填 trail → 用全局默认 trail_default(null=关, 不注入)。
    # 引擎只认 params(纯函数), 回落在这一层做 — 与实盘 runner 拉取时同一回落规则, 两边一致
    params = s["params"]
    if isinstance(params, dict) and not params.get("trail"):
        td = await pool.fetchval("SELECT value FROM config WHERE key='trail_default'")
        if isinstance(td, dict) and td.get("active"):
            params = {**params, "trail": td}
    # regime 门(v0.3): metadata 有门 → 引擎带门跑(该品种时间线, 版本钉死); 无门 → None 原路径
    gate = await regime.gate_for(pool, s["metadata"], sym, s["broker"])
    result = await asyncio.to_thread(
        backtest.run_backtest, cache["m1"], s["template"], params,
        meta["point"], s["timeframe"], oos_split=oos_split, gate=gate, **payload["costs"])
    # to_time 记"实际读到的最后一根 M1", 不用请求截止 t_to(默认=now)。
    # 否则数据滞后于 now 时跑的回测会把 to_time 记成 now, 让对账 bt_stale 误判为"新鲜"、
    # 把本该"重跑回测"的缺口错标成"真差异"(not_triggered) — 曾误导排查。
    cov_to = datetime.fromtimestamp(int(cache["m1"]["time"][-1]), tz=timezone.utc)
    # 每"策略×品种"一行, upsert 覆盖(幂等 — job 重试安全, 铁律6)
    await pool.execute(
        "INSERT INTO backtests"
        " (strategy_id, from_time, to_time, symbol, broker, metrics, trades)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7)"
        " ON CONFLICT (strategy_id, symbol, broker) DO UPDATE SET"
        "   from_time=EXCLUDED.from_time, to_time=EXCLUDED.to_time,"
        "   broker=EXCLUDED.broker, metrics=EXCLUDED.metrics,"
        "   trades=EXCLUDED.trades, created_at=now()",
        s["id"], t_from, cov_to, sym, meta["broker"], result["metrics"], result["trades"])


async def _run_judge(pool: asyncpg.Pool, job_id: int, payload: dict):
    """执行一个 oos_v2 判定任务(2026-08-08 判定下放 worker): 一块 ≤judge_chunk 个策略 —
    取回测行 → 纯计算切六段判定(挪线程, 不噎事件循环) → 明细写回本 job 的 result 列。
    api 收尾只合并各块 result 出报告 — 内存/CPU 摊到全部 worker, api 峰值从 27GB → 几十MB。"""
    from datetime import date as _date
    from src.services import oos_v2   # 函数级 import: oos_v2 顶层 import jobs, 防环
    cfg = payload["run"]
    p = cfg["judge"]
    anchor = _date.fromisoformat(cfg["anchor"])
    entries = payload["chunk"]
    errors = payload.get("errors") or {}
    ids = [int(e["id"]) for e in entries]
    strats = {r["id"]: dict(r) for r in await pool.fetch(
        "SELECT id, name, symbol, status FROM strategies WHERE id = ANY($1)", ids)}
    bt_rows = {r["strategy_id"]: dict(r) for r in await pool.fetch(
        "SELECT b.strategy_id, b.trades FROM backtests b"
        " JOIN strategies s ON s.id = b.strategy_id AND s.symbol = b.symbol"
        "   AND b.broker = s.broker"
        " WHERE b.strategy_id = ANY($1)", ids)}

    def _judge_all() -> list:
        out = []
        for e in entries:
            sid = int(e["id"])
            strat = strats.get(sid)
            if strat is None:   # 跑批期间被删
                out.append({"id": sid, "name": e.get("name"), "symbol": e.get("symbol"),
                            "status": "—", "verdict": "skip", "reason": "策略已删除"})
                continue
            bt = {"error": errors[str(sid)]} if str(sid) in errors else bt_rows.get(sid)
            out.append(oos_v2.judge_one(strat, bt, anchor, p))
        return out

    details = await asyncio.to_thread(_judge_all)
    await pool.execute("UPDATE jobs SET result=$2 WHERE id=$1", job_id, details)


async def _run_map(pool, job_id: int, payload: dict) -> None:
    """策略×regime 映射规律(2026-08-11): 一块 = 一批策略, 每策略对每个版本【独立】算。
    纯读库(backtests.trades 复用 + regime_timeline)+ 内存计算, 不跑引擎。
    结果写 jobs.result, 由 finalize 合并成报告。"""
    from src.services import regime_map as rmap
    p = payload["params"]
    out = []
    for sid in payload["ids"]:
        s = await pool.fetchrow(
            "SELECT s.id, s.name, s.template, s.symbol, s.broker, s.timeframe,"
            "       s.status, s.params, sy.point"
            "  FROM strategies s"
            " LEFT JOIN symbols sy ON sy.symbol = s.symbol AND sy.broker = s.broker"
            " WHERE s.id=$1", sid)
        if s is None:
            continue
        bt = await pool.fetchrow(
            "SELECT from_time, to_time, trades FROM backtests"
            " WHERE strategy_id=$1 AND symbol=$2 AND broker=$3",
            sid, s["symbol"], s["broker"])
        base = {"id": sid, "name": s["name"], "template": s["template"],
                "symbol": s["symbol"], "timeframe": s["timeframe"], "status": s["status"],
                # slow = 池化分档用(持仓长短的代理: 持仓 ≈ 0.8×slow 根)
                "slow": (s["params"] or {}).get("slow")}
        if bt is None or not (bt["trades"] or []):
            out.append({**base, "verdict": "skip", "reason": "缺回测行(先跑一发回测)"})
            continue
        # CPU 密集(置换检验) → 扔线程池: api 进程内也消费任务, 同步跑会卡死事件循环
        # (progress 都查不动); 每策略之间还让出一次控制权
        rows, counts = await asyncio.to_thread(
            rmap.classify, bt["trades"], p, float(s["point"] or 0.01))
        n = len(rows)
        base["window"] = f"{bt['from_time']:%Y-%m-%d} ~ {bt['to_time']:%Y-%m-%d}"
        base["n"] = n
        base["tiers"] = counts        # 不分格的四类画像(策略自身长相)
        base["tier_pct"] = {k: round(v / n * 100, 1) for k, v in counts.items()} if n else {}
        vers = {}
        for v in payload["versions"]:
            tl = {r["date"]: r["regime"] for r in await pool.fetch(
                "SELECT date, regime FROM regime_timeline"
                " WHERE version_id=$1 AND symbol=$2"
                "   AND broker=(SELECT broker FROM strategies WHERE id=$3)",
                v, s["symbol"], sid)}
            # 独立评估: seed 用 策略id*100+版本, 结果可复现且各版本互不影响
            vers[str(v)] = await asyncio.to_thread(
                rmap.analyze_version, rows, tl, p, sid * 100 + v)
        base["versions"] = vers
        await asyncio.sleep(0)          # 让出控制权, api 能继续响应请求
        base["verdict"] = ("signal" if any(x.get("verdict") == "signal" for x in vers.values())
                           else "weak" if any(x.get("verdict") == "weak" for x in vers.values())
                           else "none")
        out.append(base)
    await pool.execute("UPDATE jobs SET result=$2 WHERE id=$1", job_id, out)


async def _run_screen_judge(pool: asyncpg.Pool, job_id: int, payload: dict):
    """执行一个 v1(regime_screen) 判定任务(2026-08-08 统一下放): 一块 ≤judge_chunk 个策略 —
    取回测行 → 逐笔贴时间线切片判定(共用 services/screen 唯一判定) → [明细, 动作] 写回 result。
    时间线按 (品种×版本) 在块内缓存(tls), 十来个品种各取一次。判定循环含 await(取时间线),
    不挪线程 — worker 消费者本就单工干活, 阻塞自己无碍。"""
    from src.services import screen   # 函数级 import: screen 顶层 import jobs, 防环
    cfg = payload["run"]
    p = cfg["judge"]
    vid = int(cfg["version"])
    symbols_mode = cfg["symbols"]
    need_days = int(p["window_years"] * 365.25) - 45
    entries = payload["chunk"]
    errors = payload.get("errors") or {}
    ids = [int(e["id"]) for e in entries]
    strats = {r["id"]: dict(r) for r in await pool.fetch(
        "SELECT id, name, symbol, status FROM strategies WHERE id = ANY($1)", ids)}
    bt_by_sid: dict = {}
    for r in await pool.fetch(
            "SELECT b.strategy_id, b.symbol, b.from_time, b.to_time, b.trades"
            "  FROM backtests b JOIN strategies s ON s.id = b.strategy_id"
            "   AND b.broker = s.broker"      # v2.3: 只判户口券商的行(含跨品种)
            " WHERE b.strategy_id = ANY($1)", ids):
        bt_by_sid.setdefault(r["strategy_id"], []).append(dict(r))
    tls: dict = {}
    out = []
    for e in entries:
        sid = int(e["id"])
        strat = strats.get(sid)
        if strat is None:   # 跑批期间被删
            out.append([{"id": sid, "name": e.get("name"), "symbol": e.get("symbol"),
                         "status": "—", "verdict": "skip", "reason": "策略已删除"}, None])
            continue
        res_map: dict = {}
        for sym, err in (errors.get(str(sid)) or {}).items():
            res_map[sym] = {"error": err}
        for bt in bt_by_sid.get(sid, []):
            sym = bt["symbol"]
            if sym in res_map:
                continue          # 该品种任务失败: 保留失败态, 不用旧行冒充
            if symbols_mode == "main" and sym != strat["symbol"]:
                continue          # 主货币模式只判主品种
            if (bt["to_time"] - bt["from_time"]).days < need_days:
                res_map[sym] = None   # 窗口不足: 主品种→跳过; 跨品种→不纳入要求
                continue
            res_map[sym] = await screen.judge_symbol(pool, tls, bt, vid, p)
        if strat["symbol"] not in res_map:
            res_map[strat["symbol"]] = None   # 主品种连回测行都没有
        d, action = screen.judge_one(strat, res_map, p, symbols_mode)
        out.append([d, action])
    await pool.execute("UPDATE jobs SET result=$2 WHERE id=$1", job_id, out)


async def consumer_loop(pool: asyncpg.Pool):
    """常驻消费者(api 内 1 路 + worker 容器 N 路, 同一函数):
    抢单(SKIP LOCKED, 按品种排序) → 执行 → DONE/FAILED; 空队列时低频轮询 + 顺手回收租约。
    认 ENGINE_KINDS 全部工种(backtest / regime_screen): 都是"跑一次回测"的活, 执行路径同一份
    (_run_one) → 结果必然一致(尺子不变); 两队列并存时先清回测(kind 排序), 只影响先后不影响正确性。"""
    cache: dict = {}
    logger.info("jobs consumer started (%s), kinds=%s", WORKER, ",".join(ENGINE_KINDS))
    while True:
        try:
            await _reclaim(pool)
            job = await pool.fetchrow(
                "UPDATE jobs SET status='RUNNING', worker=$1,"
                "   started_at=now(), attempts=attempts+1"
                " WHERE id = (SELECT id FROM jobs WHERE kind = ANY($2) AND status='PENDING'"
                "             ORDER BY kind, payload->>'symbol', id LIMIT 1"
                "             FOR UPDATE SKIP LOCKED)"
                " RETURNING id, kind, payload, attempts", WORKER, list(CLAIM_KINDS))
            if job is None:
                cache.clear()   # 队列空: 释放缓存的 M1(可能几百MB), 再睡
                await asyncio.sleep(POLL_SECONDS)
                continue
            try:
                if job["kind"] == OOS_JUDGE_KIND:   # 判定块(纯计算) vs 回测(引擎)
                    await _run_judge(pool, job["id"], job["payload"])
                elif job["kind"] == SCREEN_JUDGE_KIND:
                    await _run_screen_judge(pool, job["id"], job["payload"])
                elif job["kind"] == MAP_KIND:       # 映射规律: 纯计算(复用回测行, 不跑引擎)
                    await _run_map(pool, job["id"], job["payload"])
                else:
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
