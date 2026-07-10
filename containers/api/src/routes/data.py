"""/syncdata + /config — 历史数据下载与系统配置

职责: 触发/查询数据同步(逻辑在 services.sync)、数据覆盖统计、
     系统配置读写(下载品种/起始日期, 存 config 表, web 可改)。

扩展点: 新配置项 = CONFIG_KEYS 加 key + 校验分支 + postgres/schema/ 新增幂等种子 SQL。
"""
import asyncio
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import sync

router = APIRouter()

CONFIG_KEYS = {"symbols", "data_start", "ai_generator_url", "backtest_costs"}


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


@router.get("/syncdata/coverage")
async def data_coverage(request: Request):
    """每个品种已入库的数据范围"""
    rows = await request.app.state.pool.fetch(
        "SELECT symbol, min(time) AS first_bar, max(time) AS last_bar, count(*) AS bars"
        "  FROM historical_bars WHERE timeframe='M1' GROUP BY symbol ORDER BY symbol")
    return {"coverage": [dict(r) for r in rows]}


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
    if key == "symbols":
        if not isinstance(req.value, list) or not all(isinstance(s, str) and s for s in req.value):
            raise HTTPException(status_code=400, detail="symbols must be a list of strings")
        req.value = [s.strip().upper() for s in req.value]
    if key == "data_start":
        try:
            date.fromisoformat(str(req.value))
        except ValueError:
            raise HTTPException(status_code=400, detail="data_start must be YYYY-MM-DD")
    if key == "backtest_costs":
        if not isinstance(req.value, dict):
            raise HTTPException(status_code=400, detail="backtest_costs must be an object")
        for k in ("slippage_points", "commission_points"):
            if not isinstance(req.value.get(k), (int, float)):
                raise HTTPException(status_code=400, detail=f"backtest_costs.{k} must be a number")
        sp = req.value.get("spread_points")
        if sp is not None and not isinstance(sp, (int, float)):
            raise HTTPException(status_code=400, detail="spread_points must be number or null")
    await request.app.state.pool.execute(
        "INSERT INTO config (key, value) VALUES ($1, $2)"
        " ON CONFLICT (key) DO UPDATE SET value = $2", key, req.value)
    return {key: req.value}
