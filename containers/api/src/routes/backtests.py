"""/backtest — 批量回测的调度与结果查询

职责: 挑选策略批次、按品种分组加载 M1(每品种只加载一次)、调用回测引擎、
     结果入库、排名查询。撮合规则本体在 services/backtest.py。

扩展点: 新增回测指标 = services/backtest.py 的 _metrics() 加字段
       (metrics 是 JSONB, 表结构不用动)。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import backtest

logger = logging.getLogger("backtests")
router = APIRouter()

# 全局进度 (单进程内存即可)
bt_state = {"running": False, "current": None, "done": 0, "total": 0, "errors": []}


class BacktestRequest(BaseModel):
    status: str = "CANDIDATE"          # 回测哪批策略
    symbol: Optional[str] = None
    strategy_ids: Optional[list[int]] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    limit: int = 500
    # 成本模型: 不传则用 config 表 backtest_costs 的系统默认 (web 可改)
    slippage_points: Optional[float] = None
    commission_points: Optional[float] = None
    spread_points: Optional[float] = None  # null=用bar记录的真实点差


@router.post("/backtest/run")
async def run(req: BacktestRequest, request: Request):
    """批量回测 (后台执行)"""
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
    # 成本: 请求值 > config 系统默认 > 代码默认
    cfg = await pool.fetchval("SELECT value FROM config WHERE key='backtest_costs'") or {}
    costs = {
        "slippage_points": req.slippage_points if req.slippage_points is not None
                           else cfg.get("slippage_points", backtest.DEFAULT_SLIPPAGE_POINTS),
        "commission_points": req.commission_points if req.commission_points is not None
                             else cfg.get("commission_points", backtest.DEFAULT_COMMISSION_POINTS),
        "spread_points": req.spread_points if req.spread_points is not None
                         else cfg.get("spread_points"),
    }
    asyncio.create_task(_run_batch(pool, [dict(r) for r in rows],
                                   req.from_time, req.to_time, costs))
    return {"started": True, "total": len(rows), "costs": costs}


async def _run_batch(pool, strategies: list, t_from, t_to, costs: dict):
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
                raise ValueError(f"no M1 data for {s['symbol']}, run /syncdata first")

            result = await asyncio.to_thread(
                backtest.run_backtest, m1, s["template"], s["params"],
                points[s["symbol"]], s["timeframe"], **costs)
            await pool.execute(
                "INSERT INTO backtests (strategy_id, from_time, to_time, metrics, trades)"
                " VALUES ($1, $2, $3, $4, $5)",
                s["id"], t_from, t_to, result["metrics"], result["trades"])
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
    """每个策略取最新一次回测, 按净点数排名"""
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
    """单策略的历史回测记录"""
    rows = await request.app.state.pool.fetch(
        "SELECT id, from_time, to_time, metrics, created_at FROM backtests"
        " WHERE strategy_id=$1 ORDER BY created_at DESC", strategy_id)
    return {"results": [dict(r) for r in rows]}
