"""/syncdata + /config — 历史数据下载与系统配置

职责: 触发/查询数据同步(逻辑在 services.sync)、数据覆盖统计、系统配置读写。

品种清单/起始日期不在这里 — 品种唯一数据源是 symbols 表(见 routes/symbols.py)。
扩展点: 新配置项 = CONFIG_KEYS 加 key + 校验分支 + postgres/schema/ 新增幂等种子 SQL。
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import regime, sync

logger = logging.getLogger("data")

router = APIRouter()


# ---------- 市场状态 Regime(v2.5 阶段一: 尺子先行, 只读展示, 无任何选拔逻辑) ----------
# 注意: /regime/evaluate 必须注册在 /regime/{symbol} 之前, 否则 'evaluate' 被当品种名解析。
# 候选口径族(v2.5 评定流程, 文档定死 ≤8 组): 长 {SMA200/EMA200} × 短 {SMA20/SMA50}
# × 波动分位 {0.5 中位/0.67 前三分之一}; ATR14/252日窗固定。评定完成后选定一组冻结。
EVAL_CANDIDATES = [
    {"long_ma": lm, "short_ma": sm, "atr_n": 14, "vol_win": 252, "vol_q": q}
    for lm in ("sma200", "ema200") for sm in ("sma20", "sma50") for q in (0.5, 0.67)]


def _eval_key(p: dict) -> str:
    """口径规范串(缓存主键): 长|短|atr|窗|分位 — 与 schema/052 注释同格式"""
    return f"{p['long_ma']}|{p['short_ma']}|{p['atr_n']}|{p['vol_win']}|{p['vol_q']:g}"


async def _eval_one(pool, cand: dict, d1s: dict) -> dict:
    """现算一个候选口径 × 已载入的各品种 D1 → per_symbol {stats, distinct}"""
    per = {}
    for sym, (dates, h, low, c) in d1s.items():
        dims, start = regime.compute_regimes(h, low, c, cand)
        if dims is None or start >= len(dates):
            continue
        regs = [dims[0][i] + dims[1][i] + dims[2][i] for i in range(start, len(dates))]
        per[sym] = {"stats": regime.stats(regs),
                    "distinct": regime.distinct(h, low, c, dims, start)}
    return per


@router.get("/regime/evaluate")
async def regime_evaluate(request: Request):
    """候选口径族对比(v2.5 评定流程"记分卡"): 8 候选 × 全部下载品种。
    默认(无 refresh)=纯读缓存(regime_eval_cache, 按口径存一份), 加载即出不动数据;
    refresh=missing=只对缓存缺失的候选现算并入库(全部命中=秒回);
    refresh=all=8 候选全部重扫全品种 D1 覆盖缓存(数据更新/加品种后用)。
    评分是调试期只读产物, 不进交易/归因链路, 换配置/加品种无残留(手动重算即最新)。"""
    pool = request.app.state.pool
    symbols = [r["symbol"] for r in await pool.fetch(
        "SELECT symbol FROM symbols WHERE download ORDER BY symbol")]
    cached = {r["params_key"]: r for r in await pool.fetch(
        "SELECT params_key, params, symbols, per_symbol, computed_at FROM regime_eval_cache")}
    refresh = str(request.query_params.get("refresh", "")).lower()
    # 只有显式 refresh 才现算(否则纯读缓存, 空缓存=空结果, 页面提示去重算):
    #   all/force=全部重算; missing=只补缺失候选
    if refresh in ("all", "force"):
        to_compute = list(EVAL_CANDIDATES)
    elif refresh == "missing":
        to_compute = [c for c in EVAL_CANDIDATES if _eval_key(c) not in cached]
    else:
        to_compute = []
    d1s, skipped = {}, []
    if to_compute:
        need = max(regime.warmup_days(c) for c in EVAL_CANDIDATES) + 260
        for sym in symbols:
            d1 = await regime._d1(pool, sym, need)
            if d1 is None:
                skipped.append(sym)
            else:
                d1s[sym] = d1
        for cand in to_compute:
            per = await _eval_one(pool, cand, d1s)
            await pool.execute(
                "INSERT INTO regime_eval_cache (params_key, params, symbols, per_symbol,"
                "   computed_at) VALUES ($1, $2, $3, $4, now())"
                " ON CONFLICT (params_key) DO UPDATE SET params=$2, symbols=$3,"
                "   per_symbol=$4, computed_at=now()",
                _eval_key(cand), cand, sorted(d1s), per)
            cached[_eval_key(cand)] = {"params": cand, "symbols": sorted(d1s),
                                       "per_symbol": per, "computed_at": None}
    # 组装(全部从 cached 出, 保持 EVAL_CANDIDATES 顺序)
    out, all_syms = [], set()
    for cand in EVAL_CANDIDATES:
        row = cached.get(_eval_key(cand))
        per = (row["per_symbol"] if row else {}) or {}
        all_syms |= set(per)
        out.append({
            "label": f"{cand['long_ma'].upper()}/{cand['short_ma'].upper()}·q{cand['vol_q']:g}",
            "params": cand, "per_symbol": per,
            "cached_symbols": (row["symbols"] if row else []) or [],
            "computed_at": (row["computed_at"].isoformat()
                            if row and row.get("computed_at") else None)})
    return {"candidates": out, "symbols": sorted(all_syms), "skipped": skipped,
            "current_symbols": symbols}   # 当前下载品种集(与 cached_symbols 比 = 缓存是否旧)



def _validate_regime_params(value) -> dict:
    """Regime 口径参数校验(键完整 + 均线格式 + 数值区间) — 版本创建唯一入口用"""
    want = {"long_ma", "short_ma", "atr_n", "vol_win", "vol_q"}
    if not isinstance(value, dict) or set(value) != want:
        raise HTTPException(status_code=400, detail=f"口径键必须恰好是 {sorted(want)}")
    try:   # 周期区间与配置页 number 框一致(前后端同一边界, 不给幻想):
        n_long = regime.parse_ma(value["long_ma"])[1]    # 长趋势 20~500
        n_short = regime.parse_ma(value["short_ma"])[1]  # 短趋势 5~100
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not 20 <= n_long <= 500:   # 下限20: 短历史调试用(正式评定用200级)
        raise HTTPException(status_code=400, detail="长趋势均线周期须为 20~500 交易日")
    if not 5 <= n_short <= 100:
        raise HTTPException(status_code=400, detail="短趋势均线周期须为 5~100 交易日")
    if not isinstance(value["atr_n"], int) or not 2 <= value["atr_n"] <= 100:
        raise HTTPException(status_code=400, detail="atr_n 须为 2~100 的整数")
    if not isinstance(value["vol_win"], int) or not 20 <= value["vol_win"] <= 1000:
        raise HTTPException(status_code=400, detail="vol_win 须为 20~1000 的整数(交易日)")
    q = value["vol_q"]
    if not isinstance(q, (int, float)) or not 0.1 <= q <= 0.9:
        raise HTTPException(status_code=400, detail="vol_q 须为 0.1~0.9(高波阈值分位)")
    return value


async def _version_save(pool, params: dict) -> dict:
    """一套参数=一个版本(params UNIQUE 判重执法): 新参数→新版本; 撞上→匹配现有版本。
    保存即设为当前默认(config regime_version, 一处)。"""
    # 先查后插: ON CONFLICT 会"先取号再撞墙"(序列非事务), 重复保存白烧版本号导致跳号 —
    # 先 SELECT 命中就直接返回, 序列一号不动; 真新参数才 INSERT(并发撞车兜底再查一次)
    vid = await pool.fetchval("SELECT id FROM regime_versions WHERE params=$1", params)
    created = False
    if vid is None:
        row = await pool.fetchrow(
            "INSERT INTO regime_versions (params) VALUES ($1)"
            " ON CONFLICT (params) DO NOTHING RETURNING id", params)
        if row is not None:
            vid, created = row["id"], True
        else:   # 极小概率并发撞车: 另一个请求刚插完 → 拿它的
            vid = await pool.fetchval(
                "SELECT id FROM regime_versions WHERE params=$1", params)
    await pool.execute(
        "INSERT INTO config (key, value) VALUES ('regime_version', $1)"
        " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", vid)
    return {"id": vid, "created": created, "params": params}


@router.get("/regime/versions")
async def regime_versions_list(request: Request):
    """版本清单(下拉用) + 当前默认。页面无删除口(v0.2): 删错版本=Frank 直接
    DELETE FROM regime_versions WHERE id=N(级联清时间线), 自愈回落见 regime.active_version"""
    pool = request.app.state.pool
    vid, _ = await regime.active_version(pool)
    rows = await pool.fetch(
        "SELECT id, params, created_at FROM regime_versions ORDER BY id")
    return {"current": vid,
            "versions": [{"id": r["id"], "params": r["params"],
                          "created_at": r["created_at"].isoformat()} for r in rows]}


class RegimeVersionSave(BaseModel):
    params: dict


@router.post("/regime/versions")
async def regime_version_save(req: RegimeVersionSave, request: Request):
    """保存口径 → 版本化(v0.2): 新参数生成 v{新id}; 重复参数匹配回现有版本(提示"这是vN")。
    只存不重建 — 重建仍是 Regime 页显式动作(对当前默认版本)。"""
    return await _version_save(request.app.state.pool,
                               _validate_regime_params(req.params))


class RegimeVersionSelect(BaseModel):
    id: int


@router.post("/regime/versions/select")
async def regime_version_select(req: RegimeVersionSelect, request: Request):
    """切换当前默认版本(下拉选中即生效): 全部读时贴格/自愈重建随之走该版本时间线"""
    pool = request.app.state.pool
    p = await pool.fetchval("SELECT params FROM regime_versions WHERE id=$1", req.id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"版本 v{req.id} 不存在")
    await pool.execute(
        "INSERT INTO config (key, value) VALUES ('regime_version', $1)"
        " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", req.id)
    return {"current": req.id, "params": p}


@router.get("/regime/{symbol}")
async def regime_timeline(symbol: str, request: Request, days: int = 90, full: int = 0):
    """品种的 regime 时间线 + 四标准统计(页面即记分卡)。读时自愈补算(无定时任务)。
    days=演变表返回最近 N 天(色带固定给最近365天); full=1 附算区分度(标准③,
    要重扫 M1 聚合 D1, 几秒~几十秒, 口径评定时手动点, 平时不算)。"""
    name = symbol.strip().upper()
    pool = request.app.state.pool
    if not await pool.fetchval("SELECT 1 FROM symbols WHERE symbol=$1", name):
        raise HTTPException(status_code=404, detail=f"品种 {name} 未登记")
    vid, params = await regime.active_version(pool)
    err = await regime.ensure_timeline(pool, name)
    rows = await pool.fetch(
        "SELECT date, regime FROM regime_timeline"
        " WHERE version_id=$1 AND symbol=$2 ORDER BY date", vid, name)
    if not rows:
        return {"symbol": name, "error": err or "无数据", "rows": [],
                "stats": {}, "params": params, "version": vid}
    regs = [r["regime"] for r in rows]
    out = {"symbol": name, "error": err, "params": params, "version": vid,
           "stats": regime.stats(regs),
           # 最新价(库内最后一根 M1 收盘, 券商时间口径) — 页面当前状态条显示"今日 xxx"
           "last_close": await pool.fetchval(
               "SELECT close FROM historical_bars WHERE symbol=$1 AND timeframe='M1'"
               " ORDER BY time DESC LIMIT 1", name),
           "current": {"date": rows[-1]["date"].isoformat(), "regime": rows[-1]["regime"]},
           # 色带与演变表同源, 全量返回(2026-07-29: 色带跨度可选1~20年, 前端切片; 20年≈5200行)
           "rows": [{"date": r["date"].isoformat(), "regime": r["regime"]}
                    for r in rows]}
    if full:  # 标准③区分度: 算法在 services/regime.distinct(与候选族对比共用)
        d1 = await regime._d1(pool, name, regime.warmup_days(params))
        if d1 is not None:
            dates, h, low, c = d1
            dims, start = regime.compute_regimes(h, low, c, params)
            d = regime.distinct(h, low, c, dims, start)
            if d is not None:
                out["distinct"] = d
    return out


@router.post("/regime/params/reset")
async def regime_params_reset(request: Request):
    """口径恢复默认(SMA200/SMA20/ATR14/252/0.5) — 唯一权威 = services/regime.DEFAULT_PARAMS。
    版本化后语义 = 匹配/创建默认参数的版本并设为当前(通常就是 v1)。"""
    r = await _version_save(request.app.state.pool, dict(regime.DEFAULT_PARAMS))
    return {"params": regime.DEFAULT_PARAMS, "id": r["id"], "created": r["created"]}


@router.post("/regime/rebuild")
async def regime_rebuild(request: Request, symbol: str | None = None):
    """按当前默认版本口径重算时间线 — 覆盖更新不删表(同主键 UPSERT + 修剪头部残留),
    只动本版本的行。symbol=某品种只重建它(Regime 页按钮, 重建是显式动作);
    不传=全部下载品种。逐品种给结果, 失败原因如实带回。"""
    pool = request.app.state.pool
    vid, params = await regime.active_version(pool)
    if symbol:
        symbols = [symbol.strip().upper()]
    else:
        symbols = [r["symbol"] for r in await pool.fetch(
            "SELECT symbol FROM symbols WHERE download ORDER BY symbol")]
    results = {}
    for s in symbols:
        try:
            results[s] = await regime.rebuild_symbol(pool, s, params, vid) or "ok"
        except Exception as e:   # 单品种失败不挡整批, 原因如实回
            logger.warning("regime rebuild %s failed: %s: %s", s, type(e).__name__, e)
            results[s] = f"失败: {type(e).__name__}: {e}"
    return {"params": params, "version": vid, "results": results,
            "ok": sum(1 for v in results.values() if v == "ok"), "total": len(symbols)}

CONFIG_KEYS = {"backtest_costs", "backtest_batch_limit", "generate_batch_limit",
               "ranking_templates", "backtest_oos_split", "mt5_trades_days",
               "runtime_write_minutes", "runtime_gap_minutes", "cross_symbol_gate",
               "recon_pair_tol_minutes", "volume_presets", "volume_default",
               "trail_default",   # 移动止损全局默认(v0.9): null=关; 结构见 strategy_core/trailing.py
               "worker_params",   # worker 上报节奏/批量(v7.2, schema/046): announce 应答下发
               # regime_params 已退役(053 版本化): 口径唯一入口 = POST /regime/versions;
               # config 只留 regime_version 指针(由版本端点维护, 不走通用 PUT)
               "download_timeframes",  # 下载周期层(2026-07-29, schema/049): M1 必含 + 可选高周期
               "auto_sync_hours",  # 自动增量同步间隔(2026-08-01, schema/055): admin 页面可改, 0=关
               "backtest_window_days"}  # 批量回测默认窗口天数(2026-07-29, schema/051)

# worker_params 各项允许区间(用户按网络自调, 区间防脚枪):
# heartbeat 上限 60 = 轮询侧"新鲜推送"窗口 75s 的安全边界(推得比窗口慢会推/拉来回抖)
WORKER_PARAM_RANGES = {"heartbeat_seconds": (10, 60), "announce_seconds": (30, 300),
                       "bars_batch": (1000, 200000), "decision_keep_days": (3, 90),
                       # 下载节流(2026-07-29, schema/050): 每拉 N 根歇一会(0=不歇),
                       # 首灌深历史防 CPU 打满/心跳饿死; 歇息秒数 5~600
                       "dl_rest_bars": (0, 5_000_000), "dl_rest_secs": (5, 600)}


# ---------- 数据同步 ----------
class SyncRequest(BaseModel):
    # 本次同步只下这些周期层(2026-07-29 下载页勾选): None/空 = 配置的全部层。
    # 必须是配置 download_timeframes 的子集 — 下载页选项即由配置生成, 不给幻想
    timeframes: list[str] | None = None


@router.post("/syncdata")
async def start_sync(request: Request, req: SyncRequest | None = None):
    """触发全量/增量同步(断点续传)。v7.2 收口后唯一路径 = jobs 模式:
    api 只写任务表, download worker 轮询领取 + 自拉 MT5 + 分批上传(单向)。"""
    pool = request.app.state.pool
    capable = await pool.fetchval(
        "SELECT count(*) FROM mt5_hosts WHERE enabled AND download AND last_health ? 'dl_poll'")
    if not capable:
        raise HTTPException(status_code=400,
                            detail="没有会领任务的下载 worker 在线(需 worker 更新到含下载循环的版本"
                                   "并上报心跳) — 先更新/启动 worker 再同步")
    if await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM jobs WHERE kind=$1"
            " AND status IN ('PENDING','RUNNING'))", sync.DOWNLOAD_KIND):
        raise HTTPException(status_code=409, detail="download jobs already running")
    res = await sync.submit_download_jobs(
        pool, only_tfs=(req.timeframes if req else None))
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    # 触发来源留痕(2026-08-01 Frank 定样版): 手动记"谁点的"; 也重置自动计时
    # (刚手动同步完, 数据已新鲜, 自动班从此刻重新起算 — 与 _auto_sync_tick 共用同一个键)
    uid = getattr(request.state, "user_id", None)
    uname = (await pool.fetchval("SELECT name FROM users WHERE id=$1", uid)) if uid else None
    await sync.record_trigger(pool, "manual", user=uname or (f"id{uid}" if uid else "未知"))
    return {"started": True, "mode": "jobs", **res}


@router.get("/syncdata/status")
async def sync_status(request: Request):
    """进度: 下载 jobs 拼视图(与旧内存 state 同形, 下载页零改动); 无任何 jobs = 空态"""
    return (await sync.download_progress(request.app.state.pool)
            or {"running": False, "current": {}, "symbols": [],
                "bars_written": 0, "done": [], "errors": []})


@router.get("/download/task")
async def download_task(request: Request, name: str):
    """worker 领下载任务(v7.2 #3): SKIP LOCKED 抢单, 多 download 机自动分摊。
    无任务 → {"task": null}; 拒领时给明确原因(未注册/停用/无下载职能)。"""
    pool = request.app.state.pool
    h = await pool.fetchrow(
        "SELECT id, enabled, download, mt5_server, last_health FROM mt5_hosts WHERE name=$1",
        name)
    if h is None:
        raise HTTPException(status_code=404, detail=f"worker {name} 未注册 — 等 announce 建档")
    if not h["enabled"]:
        raise HTTPException(status_code=403, detail=f"worker {name} 已停用, 不派任务")
    if not h["download"]:
        raise HTTPException(status_code=403, detail=f"worker {name} 无下载职能, 不派任务")
    # 防污染保险①(2026-07-29 实证补): 老版 worker 不认识任务的 timeframe 字段, 领了 D1 任务
    # 照拉 M1 → 被错贴 D1 标签入库(XAUUSD 686万根假D1)。非 M1 任务只派给心跳里带
    # dl_tf 能力标记的新 worker; 老 worker 只配领 M1(它只会拉 M1, 标签永远是对的)。
    tf_capable = bool((h["last_health"] or {}).get("dl_tf"))
    # 券商匹配领单(纪律: 数据从实际交易的券商下载): server 来自心跳同步的登录账户
    row = await sync.claim_download_job(pool, name, h["mt5_server"], tf_capable)
    if row is None:
        return {"task": None}
    return {"task": {"job_id": row["id"], **row["payload"]}}


class BarsUpload(BaseModel):
    job_id: int
    bars: list = []         # bridge /rates 同款字段: time/open/high/low/close/tick_volume/spread/real_volume
    done: bool = False      # true = 该任务最后一批
    error: str | None = None   # worker 侧确定性失败(如券商无此品种): job 记 FAILED+原因


@router.post("/download/bars")
async def download_bars(req: BarsUpload, request: Request):
    """worker 分批上传 K线(v7.2 #3): 幂等入库(主键 ON CONFLICT), 断了重传零副作用;
    每批顺手续租(started_at=now, 活跃即持有 — 10分钟无上传由领取路收回重派)。"""
    pool = request.app.state.pool
    job = await pool.fetchrow(
        "SELECT id, status, payload FROM jobs WHERE id=$1 AND kind=$2",
        req.job_id, sync.DOWNLOAD_KIND)
    if job is None:   # 新一批提交时旧 jobs 被清 → 老任务作废, worker 放弃重领即可
        raise HTTPException(status_code=404,
                            detail=f"job {req.job_id} 不存在(可能已被新一批下载清掉) — 放弃本任务重新领取")
    if job["status"] != "RUNNING":
        raise HTTPException(status_code=409,
                            detail=f"job {req.job_id} 状态={job['status']} 非 RUNNING"
                                   "(可能怠工被收回重派) — 放弃本任务重新领取")
    if req.error:
        await pool.execute(
            "UPDATE jobs SET status='FAILED', error=$2, finished_at=now() WHERE id=$1",
            req.job_id, req.error[:500])
        return {"accepted": True, "failed": True}
    # 防污染保险②(2026-07-29): bar 时间必须落在任务周期的整点格上(D1=日界, H1=整点…) —
    # "M1 数据贴高周期标签"在第一批就被当场拒收记 FAILED, 不入库不扩散
    tf = job["payload"].get("timeframe", "M1")
    tf_secs = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
               "H1": 3600, "H4": 14400, "D1": 86400}.get(tf, 60)
    bad = next((b for b in req.bars if int(b.get("time", 0)) % tf_secs), None)
    if bad is not None:
        msg = (f"bar 时间 {bad['time']} 与周期 {tf} 不对齐 — 典型指纹: 老版 worker 不认识"
               " timeframe 拉了 M1 贴高周期标签; 先 update.bat 更新该 worker 再重试")
        await pool.execute(
            "UPDATE jobs SET status='FAILED', error=$2, finished_at=now() WHERE id=$1",
            req.job_id, msg)
        raise HTTPException(status_code=400, detail=msg)
    written = 0
    if req.bars:
        try:
            async with pool.acquire() as conn:
                written = await sync.insert_bars(
                    conn, job["payload"]["symbol"], req.bars,
                    job["payload"].get("timeframe", "M1"))   # D1 补头任务(2026-07-29)
        except (KeyError, TypeError, ValueError) as e:   # 形状不合法: 400 带具体字段错误
            raise HTTPException(status_code=400,
                                detail=f"bars 形状不合法({type(e).__name__}: {e}) — "
                                       "需 bridge /rates 同款字段")
    await pool.execute(
        "UPDATE jobs SET started_at=now(),"   # 续租: 有上传就不算怠工
        "  payload = jsonb_set(payload, '{written}',"
        "     to_jsonb(COALESCE((payload->>'written')::bigint, 0) + $2::bigint))"
        + (", status='DONE', finished_at=now()" if req.done else "")
        + " WHERE id=$1", req.job_id, written)
    return {"accepted": True, "written": written, "done": req.done}


# 数据覆盖已并入 GET /symbols (品种主档随附每品种 M1 覆盖), 不再单列端点


# ---------- 系统配置 ----------
@router.get("/config")
async def get_config(request: Request):
    rows = await request.app.state.pool.fetch("SELECT key, value FROM config ORDER BY key")
    return {"config": {r["key"]: r["value"] for r in rows}}


class ConfigUpdate(BaseModel):
    value: object


@router.put("/config/{key}")
async def set_config(key: str, req: ConfigUpdate, request: Request):
    if key not in CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"unknown key, allowed: {sorted(CONFIG_KEYS)}")
    if key == "backtest_costs":
        if not isinstance(req.value, dict):
            raise HTTPException(status_code=400, detail="backtest_costs must be an object")
        for k in ("slippage_points", "commission_points"):
            if not isinstance(req.value.get(k), (int, float)):
                raise HTTPException(status_code=400, detail=f"backtest_costs.{k} must be a number")
        sp = req.value.get("spread_points")
        if sp is not None and not isinstance(sp, (int, float)):
            raise HTTPException(status_code=400, detail="spread_points must be number or null")
    if key in ("backtest_batch_limit", "generate_batch_limit"):  # 单批上限(防失控保护)
        if not isinstance(req.value, int) or req.value < 1:
            raise HTTPException(status_code=400, detail=f"{key} must be a positive integer")
    if key == "volume_default":  # 默认下单手数(没设每策略手数时 runner 用它)
        if not isinstance(req.value, (int, float)) or not 0 < req.value <= 100:
            raise HTTPException(status_code=400,
                                detail="volume_default must be a number in (0, 100]")
    if key == "volume_presets":  # 手数预设(策略列表下拉选项): 正数列表, 升序不强制
        if (not isinstance(req.value, list) or not req.value
                or not all(isinstance(v, (int, float)) and 0 < v <= 100 for v in req.value)):
            raise HTTPException(status_code=400,
                                detail="volume_presets must be a non-empty list of numbers in (0, 100]")
    if key == "backtest_oos_split":  # OOS 训练段占比: (0,1) 开区间
        if not isinstance(req.value, (int, float)) or not 0 < req.value < 1:
            raise HTTPException(status_code=400, detail="backtest_oos_split must be between 0 and 1")
    if key == "cross_symbol_gate":  # 交叉测试门槛: 各项数值或 null(=不检查)
        allowed = {"min_trades", "min_win_rate", "min_net_points", "min_pf", "max_dd_points"}
        if not isinstance(req.value, dict) or set(req.value) - allowed:
            raise HTTPException(status_code=400,
                                detail=f"cross_symbol_gate keys must be subset of {sorted(allowed)}")
        for k, v in req.value.items():
            if v is not None and not isinstance(v, (int, float)):
                raise HTTPException(status_code=400,
                                    detail=f"cross_symbol_gate.{k} must be number or null")
        wr = req.value.get("min_win_rate")
        if wr is not None and not 0 <= wr <= 1:
            raise HTTPException(status_code=400, detail="min_win_rate must be 0~1 (e.g. 0.3)")
    if key == "worker_params":  # worker 上报节奏/批量: 键完整 + 各项在允许区间内
        if not isinstance(req.value, dict) or set(req.value) != set(WORKER_PARAM_RANGES):
            raise HTTPException(status_code=400,
                                detail=f"worker_params 键必须恰好是 {sorted(WORKER_PARAM_RANGES)}")
        for k, (lo, hi) in WORKER_PARAM_RANGES.items():
            v = req.value.get(k)
            if not isinstance(v, int) or not lo <= v <= hi:
                raise HTTPException(status_code=400, detail=f"worker_params.{k} 须为 {lo}~{hi} 的整数")
    if key == "backtest_window_days":  # 批量回测默认窗口(天): 30天~20年
        if not isinstance(req.value, int) or not 30 <= req.value <= 7400:
            raise HTTPException(status_code=400, detail="backtest_window_days 须为 30~7400 的整数(天)")
    if key == "download_timeframes":  # 下载周期层: M1 必含(唯一原始数据), 其余须为已知周期
        allowed_tf = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
        if (not isinstance(req.value, list) or "M1" not in req.value
                or not set(req.value) <= set(allowed_tf)):
            raise HTTPException(status_code=400,
                                detail=f"download_timeframes 须为包含 M1 的列表, 可选值 {allowed_tf}")
        req.value = [t for t in allowed_tf if t in req.value]   # 去重并按周期从细到粗定序
    if key == "auto_sync_hours":  # 自动同步间隔(小时): 0=关闭, 上限一周
        if not isinstance(req.value, int) or not 0 <= req.value <= 168:
            raise HTTPException(status_code=400, detail="auto_sync_hours 须为 0~168 的整数(小时, 0=关闭)")
    if key == "recon_pair_tol_minutes":  # 对账配对容差: 回测与实盘时间窗口差距(分钟)
        if not isinstance(req.value, int) or not 1 <= req.value <= 120:
            raise HTTPException(status_code=400,
                                detail="recon_pair_tol_minutes must be 1~120 (minutes)")
    if key in ("runtime_write_minutes", "runtime_gap_minutes"):  # 运行区间节奏: 正整数分钟
        if not isinstance(req.value, int) or not 1 <= req.value <= 1440:
            raise HTTPException(status_code=400, detail=f"{key} must be 1~1440 (minutes)")
    if key == "mt5_trades_days":  # 流水时间预设: 正整数天数列表(≤6个)
        if (not isinstance(req.value, list) or not req.value or len(req.value) > 6
                or not all(isinstance(d, int) and 0 < d <= 3650 for d in req.value)):
            raise HTTPException(status_code=400,
                                detail="mt5_trades_days must be a list of 1~6 positive ints (days)")
    if key == "ranking_templates":  # 排名模板: UI 可增删改, 结构在此把关
        if not isinstance(req.value, list) or len(req.value) > 20:
            raise HTTPException(status_code=400, detail="ranking_templates must be a list (≤20)")
        names = set()
        for t in req.value:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"].strip():
                raise HTTPException(status_code=400, detail="每个模板需要非空 name")
            if t["name"] in names:
                raise HTTPException(status_code=400, detail=f"模板名重复: {t['name']}")
            names.add(t["name"])
            ws = [t.get(k) for k in ("stable", "profit", "risk", "robust")]
            if not all(isinstance(w, (int, float)) and w >= 0 for w in ws) or sum(ws) <= 0:
                raise HTTPException(status_code=400, detail=f"{t['name']}: 四个权重需为非负数且和>0")
            mt = t.get("min_trades", 0)
            if not isinstance(mt, int) or mt < 0:
                raise HTTPException(status_code=400, detail=f"{t['name']}: min_trades 需为非负整数")
    await request.app.state.pool.execute(
        "INSERT INTO config (key, value) VALUES ($1, $2)"
        " ON CONFLICT (key) DO UPDATE SET value = $2", key, req.value)
    return {key: req.value}
