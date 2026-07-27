"""/syncdata + /config — 历史数据下载与系统配置

职责: 触发/查询数据同步(逻辑在 services.sync)、数据覆盖统计、系统配置读写。

品种清单/起始日期不在这里 — 品种唯一数据源是 symbols 表(见 routes/symbols.py)。
扩展点: 新配置项 = CONFIG_KEYS 加 key + 校验分支 + postgres/schema/ 新增幂等种子 SQL。
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import sync

router = APIRouter()

CONFIG_KEYS = {"backtest_costs", "backtest_batch_limit", "generate_batch_limit",
               "ranking_templates", "backtest_oos_split", "mt5_trades_days",
               "runtime_write_minutes", "runtime_gap_minutes", "cross_symbol_gate",
               "recon_pair_tol_minutes", "volume_presets", "volume_default",
               "trail_default",   # 移动止损全局默认(v0.9): null=关; 结构见 strategy_core/trailing.py
               "worker_params"}   # worker 上报节奏/批量(v7.2, schema/046): announce 应答下发

# worker_params 各项允许区间(用户按网络自调, 区间防脚枪):
# heartbeat 上限 60 = 轮询侧"新鲜推送"窗口 75s 的安全边界(推得比窗口慢会推/拉来回抖)
WORKER_PARAM_RANGES = {"heartbeat_seconds": (10, 60), "announce_seconds": (30, 300),
                       "bars_batch": (1000, 200000), "decision_keep_days": (3, 90)}


# ---------- 数据同步 ----------
@router.post("/syncdata")
async def start_sync(request: Request):
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
    res = await sync.submit_download_jobs(pool)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
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
        "SELECT id, enabled, download, mt5_server FROM mt5_hosts WHERE name=$1", name)
    if h is None:
        raise HTTPException(status_code=404, detail=f"worker {name} 未注册 — 等 announce 建档")
    if not h["enabled"]:
        raise HTTPException(status_code=403, detail=f"worker {name} 已停用, 不派任务")
    if not h["download"]:
        raise HTTPException(status_code=403, detail=f"worker {name} 无下载职能, 不派任务")
    # 券商匹配领单(纪律: 数据从实际交易的券商下载): server 来自心跳同步的登录账户
    row = await sync.claim_download_job(pool, name, h["mt5_server"])
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
    written = 0
    if req.bars:
        try:
            async with pool.acquire() as conn:
                written = await sync.insert_bars(conn, job["payload"]["symbol"], req.bars)
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
