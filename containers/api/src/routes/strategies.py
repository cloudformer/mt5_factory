"""/strategies — 策略实例的生成与生命周期

职责: 模板清单、批量生成(网格/随机)、列表筛选、状态流转(准入漏斗)。
策略逻辑本体在 strategy_core/ (回测与 Windows runner 共用同一份)。

扩展点: 新策略模板 = strategy_core/templates/ 加文件 + 注册 TEMPLATES,
       本文件不用改 (模板/网格都是动态读取的)。
"""
import logging
import random

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from strategy_core import TEMPLATES, TF_SECONDS, grid_combos, random_combo

logger = logging.getLogger("strategies")
router = APIRouter()


@router.get("/strategies/templates")
async def templates_list():
    """可用策略模板及其参数网格 (前端表单用)"""
    return {"templates": {name: cls.PARAM_GRID for name, cls in TEMPLATES.items()}}


class GenerateRequest(BaseModel):
    template: str
    symbols: list[str]
    timeframe: str = "M15"
    mode: str = "grid"    # grid=固定网格(有限) | random=随机采样(近乎无限)
    count: int = 50       # random 模式下每个品种生成的数量


async def _insert_instance(pool, template, symbol, timeframe, params) -> int:
    """写入一个策略实例; 重复组合返回 0 (唯一约束去重)"""
    name = f"{template}-{symbol}-{timeframe}-" + "-".join(
        f"{k}{params[k]}" for k in sorted(params))
    result = await pool.execute(
        "INSERT INTO strategies (name, template, symbol, timeframe, params)"
        " VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
        name, template, symbol, timeframe, params)
    return int(result.split()[-1])


@router.post("/strategies/generate")
async def generate(req: GenerateRequest, request: Request):
    """批量生成 CANDIDATE 实例 (重复组合自动跳过)"""
    if req.template not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"unknown template, available: {list(TEMPLATES)}")
    if req.timeframe not in TF_SECONDS:
        raise HTTPException(status_code=400, detail=f"invalid timeframe, available: {list(TF_SECONDS)}")
    if req.mode not in ("grid", "random"):
        raise HTTPException(status_code=400, detail="mode must be grid or random")

    pool = request.app.state.pool
    created, total = 0, 0
    rng = random.Random()
    for symbol in req.symbols:
        if req.mode == "grid":
            for params in grid_combos(req.template):
                total += 1
                created += await _insert_instance(pool, req.template, symbol, req.timeframe, params)
        else:  # random: 多抽一些抵消撞重, 直到凑够 count 个新实例
            made = 0
            for _ in range(req.count * 5):  # 上限防死循环
                if made >= req.count:
                    break
                params = random_combo(req.template, rng)
                if params is None:
                    break
                total += 1
                n = await _insert_instance(pool, req.template, symbol, req.timeframe, params)
                created += n
                made += n
    logger.info("generated %d strategies (%s, mode=%s)", created, req.template, req.mode)
    return {"created": created, "skipped": total - created, "mode": req.mode,
            "template": req.template, "symbols": req.symbols}


@router.get("/strategies/status")
async def list_strategies(request: Request, status: Optional[str] = None,
                          symbol: Optional[str] = None, limit: int = 100):
    """策略实例列表, 按状态/品种筛选 (Windows runner 拉任务也走这里)"""
    q = "SELECT id, name, template, symbol, timeframe, params, status, magic_number FROM strategies"
    cond, args = [], []
    if status:
        args.append(status); cond.append(f"status = ${len(args)}")
    if symbol:
        args.append(symbol); cond.append(f"symbol = ${len(args)}")
    if cond:
        q += " WHERE " + " AND ".join(cond)
    args.append(limit)
    q += f" ORDER BY id LIMIT ${len(args)}"
    rows = await request.app.state.pool.fetch(q, *args)
    return {"count": len(rows), "strategies": [dict(r) for r in rows]}


class StatusRequest(BaseModel):
    status: str  # CANDIDATE | DEMO | ACTIVE | ARCHIVED


@router.post("/strategies/{strategy_id}/status")
async def set_status(strategy_id: int, req: StatusRequest, request: Request):
    """准入漏斗状态流转; 进入 DEMO/ACTIVE 时自动分配 magic_number (100000+id)"""
    if req.status not in ("CANDIDATE", "DEMO", "ACTIVE", "ARCHIVED"):
        raise HTTPException(status_code=400, detail="invalid status")
    row = await request.app.state.pool.fetchrow(
        "UPDATE strategies SET status=$2::text,"
        " magic_number = CASE WHEN $2::text IN ('DEMO','ACTIVE')"
        "   THEN COALESCE(magic_number, 100000 + id) ELSE magic_number END"
        " WHERE id=$1 RETURNING id, name, status, magic_number",
        strategy_id, req.status)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    return dict(row)
