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
    # 跨品种验证(反过拟合, v1.3): 同参数在所有 download 品种上各回测一次, 看是普适规律还是巧合
    cross_symbol: bool = False


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
                                   req.from_time, req.to_time, costs, req.cross_symbol))
    return {"started": True, "total": len(rows), "cross_symbol": req.cross_symbol,
            "costs": costs}


async def _run_batch(pool, strategies: list, t_from, t_to, costs: dict,
                     cross_symbol: bool = False):
    """按品种分组的外层循环: 每个品种的 M1 只加载一次, 内层跑所有需要它的策略。

    单品种(默认): 每个策略只在自己主品种上跑。
    跨品种(cross_symbol): 每个策略额外在所有 download=TRUE 品种上各跑一次(反过拟合),
        每品种一行结果, 带该品种的 point/broker 标注; 排名仍只认主品种行。
    """
    t_from = t_from or datetime(2015, 1, 1, tzinfo=timezone.utc)
    t_to = t_to or datetime.now(timezone.utc)
    meta = {r["symbol"]: r for r in
            await pool.fetch("SELECT symbol, point, broker, download FROM symbols")}
    universe = [sym for sym, r in meta.items() if r["download"]] if cross_symbol else []

    # 目标 (品种 → 要在它上面跑的策略列表): 主品种必测, 勾了跨品种再并上全 universe
    by_symbol: dict[str, list] = {}
    for s in strategies:
        targets = {s["symbol"]} | set(universe)
        for sym in targets:
            by_symbol.setdefault(sym, []).append(s)

    bt_state.update(running=True, current=None, done=0,
                    total=sum(len(v) for v in by_symbol.values()), errors=[])
    for sym, strs in by_symbol.items():
        m1 = None
        if sym not in meta:
            for s in strs:
                bt_state["errors"].append(f"{s['name']} @ {sym}: symbol not in symbols table")
                bt_state["done"] += 1
            continue
        m1 = await backtest.load_m1(pool, sym, t_from, t_to)
        for s in strs:
            bt_state["current"] = f"{s['name']} @ {sym}"
            try:
                if m1 is None:
                    raise ValueError(f"no M1 data for {sym}, run /syncdata first")
                result = await asyncio.to_thread(
                    backtest.run_backtest, m1, s["template"], s["params"],
                    meta[sym]["point"], s["timeframe"], **costs)
                await pool.execute(
                    "INSERT INTO backtests"
                    " (strategy_id, from_time, to_time, symbol, broker, metrics, trades)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    s["id"], t_from, t_to, sym, meta[sym]["broker"],
                    result["metrics"], result["trades"])
            except Exception as e:
                logger.error("backtest %s @ %s failed: %s", s["name"], sym, e)
                bt_state["errors"].append(f"{s['name']} @ {sym}: {e}")
            bt_state["done"] += 1
        m1 = None  # 释放该品种 M1 再进下一个品种

    bt_state.update(running=False, current=None)
    logger.info("backtest batch finished: %d done, %d errors",
                bt_state["done"], len(bt_state["errors"]))


@router.get("/backtest/status")
async def status():
    return bt_state


@router.get("/backtest/top")
async def top(request: Request, symbol: Optional[str] = None,
              min_trades: int = 30, limit: int = 20):
    """每个策略取主品种最新一次回测, 按净点数排名; 附带跨品种健壮性摘要与明细。

    排名只认主品种行(b.symbol = s.symbol) — 跨品种验证结果不参与排名, 只喂健壮性列,
    避免拿某个巧合品种的成绩去排名。
    """
    pool = request.app.state.pool
    q = """
        SELECT DISTINCT ON (b.strategy_id)
               b.strategy_id, s.name, s.symbol, s.timeframe, s.status, b.broker,
               b.metrics, b.created_at
          FROM backtests b JOIN strategies s ON s.id = b.strategy_id
         WHERE b.symbol = s.symbol AND (b.metrics->>'trades')::int >= $1
    """
    args = [min_trades]
    if symbol:
        args.append(symbol)
        q += f" AND s.symbol = ${len(args)}"
    q += " ORDER BY b.strategy_id, b.created_at DESC"
    rows = await pool.fetch(q, *args)
    ranked = sorted(rows, key=lambda r: r["metrics"]["net_points"], reverse=True)[:limit]

    # 跨品种健壮性: 每策略每品种取最新一次, 汇总"几个品种里几个盈利" + 明细
    ids = [r["strategy_id"] for r in ranked]
    breakdown: dict[int, list] = {}
    if ids:
        brows = await pool.fetch(
            "SELECT DISTINCT ON (strategy_id, symbol)"
            "       strategy_id, symbol, broker, metrics, created_at"
            "  FROM backtests WHERE strategy_id = ANY($1)"
            " ORDER BY strategy_id, symbol, created_at DESC", ids)
        for br in brows:
            breakdown.setdefault(br["strategy_id"], []).append(dict(br))

    results = []
    for r in ranked:
        d = dict(r)
        bd = sorted(breakdown.get(r["strategy_id"], []),
                    key=lambda x: x["metrics"].get("net_points", 0), reverse=True)
        d["breakdown"] = bd
        d["ran_on"] = len(bd)   # 在几个品种上跑过 (含没触发交易的)
        traded = [x for x in bd if x["metrics"].get("trades", 0) > 0]
        d["tested"] = len(traded)   # 实际有交易的品种数 (健壮比例的分母)
        d["profitable"] = sum(1 for x in traded if x["metrics"].get("net_points", 0) > 0)
        results.append(d)
    return {"results": results}


@router.get("/backtest/results/{strategy_id}")
async def results(strategy_id: int, request: Request):
    """单策略的历史回测记录 (含跨品种验证的各品种行)"""
    rows = await request.app.state.pool.fetch(
        "SELECT id, from_time, to_time, symbol, broker, metrics, created_at FROM backtests"
        " WHERE strategy_id=$1 ORDER BY created_at DESC", strategy_id)
    return {"results": [dict(r) for r in rows]}
