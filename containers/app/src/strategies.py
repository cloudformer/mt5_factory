"""策略生成 / 回测 / 状态管理 API"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src import backtest
from strategy_core import TEMPLATES, TF_SECONDS, grid_combos

logger = logging.getLogger("strategies")
router = APIRouter()

bt_state = {"running": False, "current": None, "done": 0, "total": 0, "errors": []}


@router.get("/strategies/templates")
async def templates_list():
    """可用策略模板及其参数网格 (前端表单用)"""
    return {"templates": {name: cls.PARAM_GRID for name, cls in TEMPLATES.items()}}


# ========== 策略生成 ==========
class GenerateRequest(BaseModel):
    template: str
    symbols: list[str]
    timeframe: str = "M15"


@router.post("/strategies/generate")
async def generate(req: GenerateRequest, request: Request):
    """模板 × 参数网格 × 品种 → 批量生成 CANDIDATE 实例 (重复组合自动跳过)"""
    if req.template not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"unknown template, available: {list(TEMPLATES)}")
    if req.timeframe not in TF_SECONDS:
        raise HTTPException(status_code=400, detail=f"invalid timeframe, available: {list(TF_SECONDS)}")

    pool = request.app.state.pool
    created = 0
    for symbol in req.symbols:
        for params in grid_combos(req.template):
            name = f"{req.template}-{symbol}-{req.timeframe}-" + "-".join(
                f"{k}{params[k]}" for k in sorted(params))
            result = await pool.execute(
                "INSERT INTO strategies (name, template, symbol, timeframe, params)"
                " VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                name, req.template, symbol, req.timeframe, params,
            )
            created += int(result.split()[-1])
    logger.info("generated %d strategies (%s)", created, req.template)
    return {"created": created, "template": req.template, "symbols": req.symbols}


@router.get("/strategies/status")
async def list_strategies(request: Request, status: Optional[str] = None,
                          symbol: Optional[str] = None, limit: int = 100):
    """策略实例列表, 按状态/品种筛选 (runner 拉任务也走这里)"""
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
    """状态流转; 进入 DEMO/ACTIVE 时自动分配 magic_number (100000+id)"""
    if req.status not in ("CANDIDATE", "DEMO", "ACTIVE", "ARCHIVED"):
        raise HTTPException(status_code=400, detail="invalid status")
    row = await request.app.state.pool.fetchrow(
        "UPDATE strategies SET status=$2::text,"
        " magic_number = CASE WHEN $2::text IN ('DEMO','ACTIVE')"
        "   THEN COALESCE(magic_number, 100000 + id) ELSE magic_number END"
        " WHERE id=$1 RETURNING id, name, status, magic_number",
        strategy_id, req.status,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    return dict(row)


# ========== 回测 ==========
class BacktestRequest(BaseModel):
    status: str = "CANDIDATE"          # 回测哪批策略
    symbol: Optional[str] = None
    strategy_ids: Optional[list[int]] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    limit: int = 500


@router.post("/backtest/run")
async def run(req: BacktestRequest, request: Request):
    """批量回测 (后台执行, 按品种分组只加载一次M1)"""
    if bt_state["running"]:
        raise HTTPException(status_code=409, detail="backtest already running")

    pool = request.app.state.pool
    if req.strategy_ids:
        rows = await pool.fetch(
            "SELECT * FROM strategies WHERE id = ANY($1) ORDER BY symbol, id", req.strategy_ids)
    else:
        q = "SELECT * FROM strategies WHERE status=$1"
        args = [req.status]
        if req.symbol:
            args.append(req.symbol); q += f" AND symbol=${len(args)}"
        args.append(req.limit)
        q += f" ORDER BY symbol, id LIMIT ${len(args)}"
        rows = await pool.fetch(q, *args)
    if not rows:
        raise HTTPException(status_code=404, detail="no strategies matched")

    bt_state.update(running=True, current=None, done=0, total=len(rows), errors=[])
    asyncio.create_task(_run_batch(pool, [dict(r) for r in rows], req.from_time, req.to_time))
    return {"started": True, "total": len(rows)}


async def _run_batch(pool, strategies: list, t_from, t_to):
    t_from = t_from or datetime(2015, 1, 1, tzinfo=timezone.utc)
    t_to = t_to or datetime.now(timezone.utc)
    points = {r["symbol"]: r["point"] for r in await pool.fetch("SELECT symbol, point FROM symbols")}

    m1, m1_symbol = None, None
    for s in strategies:
        bt_state["current"] = s["name"]
        try:
            if s["symbol"] not in points:
                raise ValueError(f"symbol {s['symbol']} not in symbols table")
            if s["symbol"] != m1_symbol:  # 按品种分组, M1 只加载一次
                m1 = await backtest.load_m1(pool, s["symbol"], t_from, t_to)
                m1_symbol = s["symbol"]
            if m1 is None:
                raise ValueError(f"no M1 data for {s['symbol']}, run /sync first")

            result = await asyncio.to_thread(
                backtest.run_backtest, m1, s["template"], s["params"],
                points[s["symbol"]], s["timeframe"],
            )
            await pool.execute(
                "INSERT INTO backtests (strategy_id, from_time, to_time, metrics, trades)"
                " VALUES ($1, $2, $3, $4, $5)",
                s["id"], t_from, t_to, result["metrics"], result["trades"],
            )
        except Exception as e:
            logger.error("backtest %s failed: %s", s["name"], e)
            bt_state["errors"].append(f"{s['name']}: {e}")
        bt_state["done"] += 1

    bt_state.update(running=False, current=None)
    logger.info("backtest batch finished: %d done, %d errors",
                bt_state["done"], len(bt_state["errors"]))


@router.get("/backtest/status")
async def status():
    return bt_state


@router.get("/backtest/top")
async def top(request: Request, symbol: Optional[str] = None,
              min_trades: int = 30, limit: int = 20):
    """按净点数排名的最新回测结果"""
    q = """
        SELECT DISTINCT ON (b.strategy_id)
               b.strategy_id, s.name, s.symbol, s.timeframe, s.status,
               b.metrics, b.created_at
          FROM backtests b JOIN strategies s ON s.id = b.strategy_id
         WHERE (b.metrics->>'trades')::int >= $1
    """
    args = [min_trades]
    if symbol:
        args.append(symbol)
        q += f" AND s.symbol = ${len(args)}"
    q += " ORDER BY b.strategy_id, b.created_at DESC"
    rows = await request.app.state.pool.fetch(q, *args)
    ranked = sorted(rows, key=lambda r: r["metrics"]["net_points"], reverse=True)
    return {"results": [dict(r) for r in ranked[:limit]]}


@router.get("/backtest/results/{strategy_id}")
async def results(strategy_id: int, request: Request):
    rows = await request.app.state.pool.fetch(
        "SELECT id, from_time, to_time, metrics, created_at FROM backtests"
        " WHERE strategy_id=$1 ORDER BY created_at DESC", strategy_id)
    return {"results": [dict(r) for r in rows]}
