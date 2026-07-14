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

DEFAULT_BATCH_LIMIT = 500  # 单批上限兜底; 实际值优先读 config 表 backtest_batch_limit(配置页可改)


async def _batch_limit(pool, requested: Optional[int]) -> int:
    """单批上限: 请求值 > config(配置页) > 代码兜底"""
    if requested:
        return requested
    return await pool.fetchval(
        "SELECT value FROM config WHERE key='backtest_batch_limit'") or DEFAULT_BATCH_LIMIT


class BacktestRequest(BaseModel):
    # 筛选维度 (v1.3): 回测按货币对进行, 与策略状态无关(回测对 demo/live 零影响)。
    #   symbol=货币对(策略主品种); broker=券商(按 symbols 表的券商标签过滤品种)。
    #   都不传 = 回测全部策略。券商标签是"下载数据"时落库的, 回测只读库、与 worker 无关。
    symbol: Optional[str] = None
    broker: Optional[str] = None
    strategy_ids: Optional[list[int]] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    # 单批上限(防失控保护): 不传则用 config 表 backtest_batch_limit(配置页可改), 再兜底 500
    limit: Optional[int] = None
    # 成本模型: 不传则用 config 表 backtest_costs 的系统默认 (web 可改)
    slippage_points: Optional[float] = None
    commission_points: Optional[float] = None
    spread_points: Optional[float] = None  # null=用bar记录的真实点差
    # 跨品种验证(乙, 反过拟合空间维度): 勾了则每策略在所有 download 品种各回测一行,
    #   看是普适规律还是只在某品种巧合。不勾=只跑主品种(快)。产出健壮性列 + 每品种明细。
    cross_symbol: bool = False
    # 范围选项: True=只跑还没有回测记录(主品种行)的策略 — 补漏不重复跑; 默认 False=全部(现状)
    untested_only: bool = False


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
        # 回测不看状态(对 demo/live 零影响, 只刷新 backtests 记录)。
        # 只回测品种仍在主档里的策略: 品种已删的孤儿策略(如旧 BTCUSD)自动跳过, 不报错。
        q = "SELECT * FROM strategies WHERE symbol IN (SELECT symbol FROM symbols)"
        args = []
        if req.symbol:  # 货币对筛选
            args.append(req.symbol); q += f" AND symbol=${len(args)}"
        if req.broker:  # 券商筛选: 按品种主档的券商标签圈定品种
            args.append(req.broker)
            q += f" AND symbol IN (SELECT symbol FROM symbols WHERE broker=${len(args)})"
        if req.untested_only:  # 范围=未测试: 主品种还没有回测记录的才跑(补漏)
            q += (" AND NOT EXISTS (SELECT 1 FROM backtests b"
                  "  WHERE b.strategy_id = strategies.id AND b.symbol = strategies.symbol)")
        args.append(await _batch_limit(pool, req.limit))
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
    asyncio.create_task(_run_batch(pool, [dict(r) for r in rows], req.from_time,
                                   req.to_time, costs, req.cross_symbol))
    return {"started": True, "total": len(rows),
            "cross_symbol": req.cross_symbol, "costs": costs}


async def _run_batch(pool, strategies: list, t_from, t_to, costs: dict,
                     cross_symbol: bool = False):
    """按品种分组的外层循环: 每个品种的 M1 只加载一次, 内层跑所有需要它的策略。

    不跨品种(默认): 每个策略只在自己主品种上跑一行。
    跨品种(cross_symbol): 每个策略额外在所有 download 品种各跑一行(反过拟合空间维度)。
    每"策略×品种"一行, upsert(键 strategy_id+symbol)覆盖; 排名只认主品种行, 其余喂健壮性。
    """
    t_from = t_from or datetime(2015, 1, 1, tzinfo=timezone.utc)
    t_to = t_to or datetime.now(timezone.utc)
    meta = {r["symbol"]: r for r in
            await pool.fetch("SELECT symbol, point, broker, download FROM symbols")}
    universe = [sym for sym, r in meta.items() if r["download"]] if cross_symbol else []

    # 目标 (品种 → 要在它上面跑的策略列表): 主品种必测(排名要它), 跨品种再并上全 universe
    by_symbol: dict[str, list] = {}
    for s in strategies:
        for sym in {s["symbol"]} | set(universe):
            by_symbol.setdefault(sym, []).append(s)

    bt_state.update(running=True, current=None, done=0,
                    total=sum(len(v) for v in by_symbol.values()), errors=[])
    for sym, strs in by_symbol.items():
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
                # 每"策略×品种"一行, 有则覆盖(键 strategy_id+symbol); 表有界不随重跑增长
                await pool.execute(
                    "INSERT INTO backtests"
                    " (strategy_id, from_time, to_time, symbol, broker, metrics, trades)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7)"
                    " ON CONFLICT (strategy_id, symbol) DO UPDATE SET"
                    "   from_time=EXCLUDED.from_time, to_time=EXCLUDED.to_time,"
                    "   broker=EXCLUDED.broker, metrics=EXCLUDED.metrics,"
                    "   trades=EXCLUDED.trades, created_at=now()",
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


@router.get("/backtest/plan")
async def plan(request: Request, symbol: Optional[str] = None, broker: Optional[str] = None,
               untested_only: bool = False, cross_symbol: bool = False,
               strategy_ids: Optional[str] = None, limit: Optional[int] = None):
    """运行预览: 按当前选择数一数会跑多少 — N 个策略 × 品种 = K 次(启动前所见即所得)"""
    pool = request.app.state.pool
    limit = await _batch_limit(pool, limit)
    # extra = 主品种不在 download 集合里的策略数(跨品种时它们的主品种仍会单独跑一次)
    count_sql = ("SELECT count(*) AS n, count(*) FILTER (WHERE symbol NOT IN"
                 " (SELECT symbol FROM symbols WHERE download)) AS extra FROM strategies")
    if strategy_ids is not None:
        try:
            ids = [int(s) for s in strategy_ids.split(",") if s.strip()]
        except ValueError:
            return {"strategies": 0, "symbols_per": 1, "runs": 0}
        if not ids:
            return {"strategies": 0, "symbols_per": 1, "runs": 0}
        row = await pool.fetchrow(f"{count_sql} WHERE id = ANY($1)", ids)
    else:
        q = f"{count_sql} WHERE symbol IN (SELECT symbol FROM symbols)"
        args = []
        if symbol:
            args.append(symbol); q += f" AND symbol=${len(args)}"
        if broker:
            args.append(broker)
            q += f" AND symbol IN (SELECT symbol FROM symbols WHERE broker=${len(args)})"
        if untested_only:
            q += (" AND NOT EXISTS (SELECT 1 FROM backtests b"
                  "  WHERE b.strategy_id = strategies.id AND b.symbol = strategies.symbol)")
        row = await pool.fetchrow(q, *args)
    n = min(row["n"], limit)
    uni = await pool.fetchval("SELECT count(*) FROM symbols WHERE download") or 0
    runs = (n * uni + min(row["extra"], n)) if cross_symbol else n
    return {"strategies": n, "symbols_per": (uni if cross_symbol else 1), "runs": runs}


def _pct_scores(values: list) -> list:
    """排名百分位归一: 值→0~100(打赢了百分之几的对手)。None=没证据→0分; 并列取平均名次。
    消除量纲差异(净点几万 vs PF小数), 加权前的统一刻度。"""
    idx = [i for i, v in enumerate(values) if v is not None]
    scores = [0.0] * len(values)
    if not idx:
        return scores
    if len(idx) == 1:
        scores[idx[0]] = 100.0
        return scores
    order = sorted(idx, key=lambda i: values[i])
    n = len(idx)
    i = 0
    while i < n:  # 并列段取平均名次
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        pct = (i + j) / 2 / (n - 1) * 100
        for k in range(i, j + 1):
            scores[order[k]] = pct
        i = j + 1
    return scores


def _apply_template_scores(cands: list, tpl: dict) -> None:
    """按模板四维权重给每个候选算综合分(0~100), 写入 d['score']。
    稳定=PF(∞=无亏损按最大) / 盈利=净点 / 风险=回撤(小=好, 取负) / 健壮=跨品种盈利比(未跨品种=0分)"""
    stable = _pct_scores([m["metrics"].get("profit_factor") if m["metrics"].get("profit_factor")
                          is not None else float("inf") for m in cands])
    profit = _pct_scores([m["metrics"].get("net_points", 0) for m in cands])
    risk = _pct_scores([-(m["metrics"].get("max_dd_points") or 0) for m in cands])
    robust = _pct_scores([(m["profitable"] / m["tested"]) if m["tested"] else None for m in cands])
    w = {k: tpl.get(k, 0) for k in ("stable", "profit", "risk", "robust")}
    total = sum(w.values()) or 1
    for i, d in enumerate(cands):
        d["score"] = round((stable[i] * w["stable"] + profit[i] * w["profit"]
                            + risk[i] * w["risk"] + robust[i] * w["robust"]) / total, 1)


@router.get("/backtest/top")
async def top(request: Request, symbol: Optional[str] = None, broker: Optional[str] = None,
              min_trades: int = 0, limit: int = 20,
              q_field: Optional[str] = None, q_text: Optional[str] = None,
              min_win_rate: float = 0, min_pf: float = 0,
              max_dd: Optional[float] = None, min_robust: Optional[float] = None,
              positive_only: bool = False, rank_template: Optional[str] = None):
    """排名: 每策略取主品种成绩(b.symbol = s.symbol), 按净点数排; 附带跨品种健壮性摘要与明细。

    排名只认主品种行 — 跨品种验证结果只喂健壮性列/明细, 不参与排名。
    过滤(全部服务端查库, 不传=不限): symbol/broker(货币对/券商)、min_trades、
      min_win_rate(胜率≥, 百分数)、min_pf(PF≥; PF为null=无亏损视为通过)、
      max_dd(最大回撤≤)、positive_only(净点数>0)、min_robust(跨品种盈利比例≥, 百分数;
      设了它则没跑过跨品种的策略不通过)。
    q_field/q_text: 服务端搜索, 策略名模糊(ILIKE), ID/周期/状态精准。
    """
    pool = request.app.state.pool
    q = """
        SELECT b.strategy_id, s.name, s.symbol, s.timeframe, s.status, b.broker,
               b.metrics, b.created_at
          FROM backtests b JOIN strategies s ON s.id = b.strategy_id
         WHERE b.symbol = s.symbol AND (b.metrics->>'trades')::int >= $1
    """
    args = [min_trades]
    if symbol:
        args.append(symbol)
        q += f" AND s.symbol = ${len(args)}"
    if broker:
        args.append(broker)
        q += f" AND b.broker = ${len(args)}"
    if min_win_rate:
        args.append(min_win_rate / 100)  # 前端传百分数, metrics 存 0~1
        q += f" AND COALESCE((b.metrics->>'win_rate')::float, 0) >= ${len(args)}"
    if min_pf:
        args.append(min_pf)  # PF=null 表示无亏损(毛损为0) → 视为无穷大, 通过
        q += f" AND COALESCE((b.metrics->>'profit_factor')::float, 1e9) >= ${len(args)}"
    if max_dd is not None:
        args.append(max_dd)
        q += f" AND COALESCE((b.metrics->>'max_dd_points')::float, 0) <= ${len(args)}"
    if positive_only:
        q += " AND (b.metrics->>'net_points')::float > 0"
    # 服务端搜索: 只有策略名模糊, 其余精准
    if q_text and q_text.strip() and q_field:
        t = q_text.strip()
        if q_field == "name":
            args.append(f"%{t}%"); q += f" AND s.name ILIKE ${len(args)}"
        elif q_field == "id" and t.isdigit():
            args.append(int(t)); q += f" AND b.strategy_id = ${len(args)}"
        elif q_field == "timeframe":
            args.append(t.upper()); q += f" AND s.timeframe = ${len(args)}"
        elif q_field == "status":
            args.append(t.upper()); q += f" AND s.status = ${len(args)}"
    rows = await pool.fetch(q, *args)
    # 先全量排序; 健壮性过滤要等聚合后才能算, 所以 limit 放最后截
    ranked = sorted(rows, key=lambda r: r["metrics"]["net_points"], reverse=True)

    # 跨品种健壮性: 每策略取其所有品种行, 汇总"几个品种赚 / 几个测过" + 每品种明细
    ids = [r["strategy_id"] for r in ranked]
    breakdown: dict[int, list] = {}
    if ids:
        for br in await pool.fetch(
                "SELECT strategy_id, symbol, broker, metrics FROM backtests"
                " WHERE strategy_id = ANY($1) ORDER BY strategy_id, symbol", ids):
            breakdown.setdefault(br["strategy_id"], []).append(dict(br))

    cands = []
    for r in ranked:
        d = dict(r)
        d["score"] = None
        bd = sorted(breakdown.get(r["strategy_id"], []),
                    key=lambda x: x["metrics"].get("net_points", 0), reverse=True)
        d["breakdown"] = bd
        d["ran_on"] = len(bd)   # 在几个品种上跑过(含没触发交易的)
        traded = [x for x in bd if x["metrics"].get("trades", 0) > 0]
        d["tested"] = len(traded)   # 实际有交易的品种数(健壮比例分母)
        d["profitable"] = sum(1 for x in traded if x["metrics"].get("net_points", 0) > 0)
        if min_robust is not None:  # 健壮性≥: 没跑跨品种/没交易 → 不通过
            if not d["tested"] or d["profitable"] / d["tested"] * 100 < min_robust:
                continue
        cands.append(d)

    # 排名模板(config 可增删改): 四维百分位加权综合分排序; 并列/无模板按净点数
    if rank_template:
        tpl = next((t for t in (await pool.fetchval(
            "SELECT value FROM config WHERE key='ranking_templates'") or [])
            if t.get("name") == rank_template), None)
        if tpl:
            _apply_template_scores(cands, tpl)
            cands.sort(key=lambda d: (d["score"], d["metrics"]["net_points"]), reverse=True)
    return {"results": cands[:limit]}


@router.get("/backtest/results/{strategy_id}")
async def results(strategy_id: int, request: Request):
    """单策略各品种的回测记录(跨品种验证的每品种一行)"""
    rows = await request.app.state.pool.fetch(
        "SELECT id, from_time, to_time, symbol, broker, metrics, created_at FROM backtests"
        " WHERE strategy_id=$1 ORDER BY symbol", strategy_id)
    return {"results": [dict(r) for r in rows]}
