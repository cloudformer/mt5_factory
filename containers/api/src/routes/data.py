"""/syncdata + /config — 历史数据下载与系统配置

职责: 触发/查询数据同步(逻辑在 services.sync)、数据覆盖统计、系统配置读写。

品种清单/起始日期不在这里 — 品种唯一数据源是 symbols 表(见 routes/symbols.py)。
扩展点: 新配置项 = CONFIG_KEYS 加 key + 校验分支 + postgres/schema/ 新增幂等种子 SQL。
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import sync

router = APIRouter()

CONFIG_KEYS = {"ai_generator_url", "backtest_costs", "backtest_batch_limit",
               "ranking_templates", "backtest_oos_split", "mt5_trades_days",
               "runtime_write_minutes", "runtime_gap_minutes", "cross_symbol_gate",
               "recon_pair_tol_minutes"}


# ---------- 数据同步 ----------
@router.post("/syncdata")
async def start_sync(request: Request):
    """触发全量/增量同步 (断点续传; 品种分摊到所有下载 worker 并行)"""
    if sync.state["running"]:
        raise HTTPException(status_code=409, detail="sync already running")
    sync.state["running"] = True
    asyncio.create_task(sync.run_full_sync(request.app.state.pool))
    return {"started": True}


@router.get("/syncdata/status")
async def sync_status():
    return sync.state


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
    if key == "backtest_batch_limit":  # 单批回测上限(防失控保护)
        if not isinstance(req.value, int) or req.value < 1:
            raise HTTPException(status_code=400, detail="backtest_batch_limit must be a positive integer")
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
