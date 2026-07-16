"""/backtest — 批量回测的调度与结果查询

职责: 挑选策略批次、按品种分组加载 M1(每品种只加载一次)、调用回测引擎、
     结果入库、排名查询。撮合规则本体在 services/backtest.py。

扩展点: 新增回测指标 = services/backtest.py 的 _metrics() 加字段
       (metrics 是 JSONB, 表结构不用动)。
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import backtest
from strategy_core import TF_SECONDS

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
    # 策略模板筛选(可选): 只回测某个模板生成的实例(如只跑 intraday_multi), 不传=不限
    template: Optional[str] = None
    # 状态筛选(可选维度, 默认不限 — 回测本身与状态无关): 支持逗号多值, 如 "DEMO,LIVE"(热层每日刷新)
    status: Optional[str] = None
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
        if req.template:  # 策略模板筛选
            args.append(req.template); q += f" AND template=${len(args)}"
        if req.symbol:  # 货币对筛选
            args.append(req.symbol); q += f" AND symbol=${len(args)}"
        if req.broker:  # 券商筛选: 按品种主档的券商标签圈定品种
            args.append(req.broker)
            q += f" AND symbol IN (SELECT symbol FROM symbols WHERE broker=${len(args)})"
        if req.status:  # 状态筛选(逗号多值): 如热层每日刷新 DEMO,LIVE
            args.append([s.strip().upper() for s in req.status.split(",") if s.strip()])
            q += f" AND status = ANY(${len(args)})"
        else:  # 默认不测已淘汰(ARCHIVED)的尸体 — 要重测它们请显式选状态或按ID点名
            q += " AND status <> 'ARCHIVED'"
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
    # OOS 切分比例(训练段占比): config 可调(配置页), 兜底 0.7
    oos_split = await pool.fetchval(
        "SELECT value FROM config WHERE key='backtest_oos_split'") or 0.7
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
                    meta[sym]["point"], s["timeframe"], oos_split=oos_split, **costs)
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
               status: Optional[str] = None, template: Optional[str] = None,
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
        if template:
            args.append(template); q += f" AND template=${len(args)}"
        if symbol:
            args.append(symbol); q += f" AND symbol=${len(args)}"
        if broker:
            args.append(broker)
            q += f" AND symbol IN (SELECT symbol FROM symbols WHERE broker=${len(args)})"
        if status:
            args.append([s.strip().upper() for s in status.split(",") if s.strip()])
            q += f" AND status = ANY(${len(args)})"
        else:  # 与 run() 一致: 默认不计已淘汰的尸体
            q += " AND status <> 'ARCHIVED'"
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
    稳定=PF(∞=无亏损按最大) / 盈利=净点 / 风险=回撤(小=好, 取负) / 健壮=跨品种盈利比。
    未回测(has_bt=False)与未跨品种: 该维没证据 → None → 0分(不冒充好成绩)"""
    def _bt(m, f):
        return f(m["metrics"]) if m.get("has_bt", True) else None
    stable = _pct_scores([_bt(m, lambda x: x.get("profit_factor")
                              if x.get("profit_factor") is not None else float("inf"))
                          for m in cands])
    profit = _pct_scores([_bt(m, lambda x: x.get("net_points", 0)) for m in cands])
    risk = _pct_scores([_bt(m, lambda x: -(x.get("max_dd_points") or 0)) for m in cands])
    robust = _pct_scores([(m["profitable"] / m["tested"]) if m["tested"] else None for m in cands])
    w = {k: tpl.get(k, 0) for k in ("stable", "profit", "risk", "robust")}
    total = sum(w.values()) or 1
    for i, d in enumerate(cands):
        d["score"] = round((stable[i] * w["stable"] + profit[i] * w["profit"]
                            + risk[i] * w["risk"] + robust[i] * w["robust"]) / total, 1)


@router.get("/backtest/top")
async def top(request: Request, symbol: Optional[str] = None, broker: Optional[str] = None,
              min_trades: int = 0, limit: int = 20, status: Optional[str] = None,
              q_field: Optional[str] = None, q_text: Optional[str] = None,
              min_win_rate: float = 0, min_pf: float = 0,
              max_dd: Optional[float] = None, min_robust: Optional[float] = None,
              positive_only: bool = False, rank_template: Optional[str] = None,
              oos_pass: bool = False, template: Optional[str] = None, page: int = 1):
    """策略列表排名: 从 strategies 出发 LEFT JOIN 主品种回测 — 未回测的策略也出现(成绩为空,
    默认沉底), 列表与排名合一。跨品种结果只喂健壮性列/明细, 不参与排名。

    过滤(全部服务端查库, 不传=不限): template(模板名)/symbol/broker/status、min_trades(>0 时未回测不通过)、
      min_win_rate(百分数)、min_pf(PF=null 即无亏损视为通过)、max_dd、positive_only、
      min_robust(百分数, 未跨品种不通过) — 有成绩门槛的过滤, 未回测策略一律不通过。
    q_field/q_text: 服务端搜索, 策略名模糊(ILIKE), ID/周期/状态精准。
    """
    pool = request.app.state.pool
    q = """
        SELECT s.id AS strategy_id, s.name, s.template, s.symbol, s.timeframe, s.status,
               s.params, s.magic_number,
               COALESCE(b.broker, sy.broker) AS broker, b.metrics, b.created_at
          FROM strategies s
          LEFT JOIN backtests b ON b.strategy_id = s.id AND b.symbol = s.symbol
          LEFT JOIN symbols sy ON sy.symbol = s.symbol
         WHERE (b.id IS NOT NULL AND (b.metrics->>'trades')::int >= $1
                OR b.id IS NULL AND $1 <= 0)
    """
    args = [min_trades]  # 未回测: min_trades=0 时显示(成绩为空), >0 时不通过(没证据)
    if symbol:
        args.append(symbol)
        q += f" AND s.symbol = ${len(args)}"
    if template:
        args.append(template)
        q += f" AND s.template = ${len(args)}"
    if broker:
        args.append(broker)
        q += f" AND COALESCE(b.broker, sy.broker) = ${len(args)}"
    if status:
        args.append(status.upper())
        q += f" AND s.status = ${len(args)}"
    if min_win_rate:
        args.append(min_win_rate / 100)  # 前端传百分数, metrics 存 0~1
        q += f" AND b.id IS NOT NULL AND COALESCE((b.metrics->>'win_rate')::float, 0) >= ${len(args)}"
    if min_pf:
        args.append(min_pf)  # PF=null 表示无亏损(毛损为0) → 视为无穷大, 通过; 未回测不通过
        q += f" AND b.id IS NOT NULL AND COALESCE((b.metrics->>'profit_factor')::float, 1e9) >= ${len(args)}"
    if max_dd is not None:
        args.append(max_dd)
        q += f" AND b.id IS NOT NULL AND COALESCE((b.metrics->>'max_dd_points')::float, 0) <= ${len(args)}"
    if positive_only:
        q += " AND (b.metrics->>'net_points')::float > 0"
    if oos_pass:  # 留出段一票否决: 留出净点>0 且有交易才通过(老结果没有oos字段=不通过)
        q += (" AND COALESCE((b.metrics#>>'{oos,holdout,net_points}')::float, 0) > 0"
              " AND COALESCE((b.metrics#>>'{oos,holdout,trades}')::int, 0) > 0")
    # 服务端搜索: 只有策略名模糊, 其余精准
    if q_text and q_text.strip() and q_field:
        t = q_text.strip()
        if q_field == "name":
            args.append(f"%{t}%"); q += f" AND s.name ILIKE ${len(args)}"
        elif q_field == "id" and t.isdigit():
            args.append(int(t)); q += f" AND s.id = ${len(args)}"
        elif q_field == "timeframe":
            args.append(t.upper()); q += f" AND s.timeframe = ${len(args)}"
        elif q_field == "status":
            args.append(t.upper()); q += f" AND s.status = ${len(args)}"
    rows = await pool.fetch(q, *args)
    # 先全量排序(未回测按 -inf 沉底), 排名在完整集合上算, 最后才切页 — 保证分页不改排名语义
    ranked = sorted(rows, key=lambda r: (r["metrics"] or {}).get("net_points", float("-inf")),
                    reverse=True)

    async def _breakdown(sids):
        """跨品种健壮性明细: strategy_id → 各品种行(按净点降序)。只为需要的策略拉(分页省算力)"""
        bd: dict[int, list] = {}
        if sids:
            for br in await pool.fetch(
                    "SELECT strategy_id, symbol, broker, metrics FROM backtests"
                    " WHERE strategy_id = ANY($1) ORDER BY strategy_id, symbol", sids):
                bd.setdefault(br["strategy_id"], []).append(dict(br))
        return bd

    def _build(r, bd_map):
        d = dict(r)
        d["has_bt"] = d["metrics"] is not None  # 未回测 → 成绩列显示 '—'
        d["metrics"] = d["metrics"] or {}
        d["score"] = None
        b = sorted(bd_map.get(r["strategy_id"], []),
                   key=lambda x: x["metrics"].get("net_points", 0), reverse=True)
        d["breakdown"] = b
        d["ran_on"] = len(b)   # 在几个品种上跑过(含没触发交易的)
        traded = [x for x in b if x["metrics"].get("trades", 0) > 0]
        d["tested"] = len(traded)   # 实际有交易的品种数(健壮比例分母)
        d["profitable"] = sum(1 for x in traded if x["metrics"].get("net_points", 0) > 0)
        return d

    lo = max(page - 1, 0) * limit
    # 排名模板打分 / 健壮性过滤 都要在"完整集合"上算(百分位、过滤后计数) → 这两种情况拉全量明细;
    # 否则(默认净点排序)只切当页、只为当页拉明细 — 载荷与 DB 明细量随页大小而非总量, 大表也快
    if rank_template or min_robust is not None:
        bd_map = await _breakdown([r["strategy_id"] for r in ranked])
        cands = []
        for r in ranked:
            d = _build(r, bd_map)
            if min_robust is not None and (  # 健壮性≥: 没跑跨品种/没交易 → 不通过
                    not d["tested"] or d["profitable"] / d["tested"] * 100 < min_robust):
                continue
            cands.append(d)
        if rank_template:  # 四维百分位加权综合分排序; 并列/无模板按净点数
            tpl = next((t for t in (await pool.fetchval(
                "SELECT value FROM config WHERE key='ranking_templates'") or [])
                if t.get("name") == rank_template), None)
            if tpl:
                _apply_template_scores(cands, tpl)
                cands.sort(key=lambda d: (d["score"],
                                          d["metrics"].get("net_points", float("-inf"))),
                           reverse=True)
        total = len(cands)
        page_cands = cands[lo:lo + limit]
    else:
        total = len(ranked)
        page_rows = ranked[lo:lo + limit]
        bd_map = await _breakdown([r["strategy_id"] for r in page_rows])
        page_cands = [_build(r, bd_map) for r in page_rows]

    return {"results": page_cands, "total": total, "page": page, "page_size": limit}


@router.get("/backtest/results/{strategy_id}")
async def results(strategy_id: int, request: Request):
    """单策略各品种的回测记录(跨品种验证的每品种一行)"""
    rows = await request.app.state.pool.fetch(
        "SELECT id, from_time, to_time, symbol, broker, metrics, created_at FROM backtests"
        " WHERE strategy_id=$1 ORDER BY symbol", strategy_id)
    return {"results": [dict(r) for r in rows]}


# ---------- 关2 对账: 回测 vs 实盘(demo/live)逐笔匹配 (v1.6) ----------
def _reconcile_metrics(actual: list, bt: list, tf_seconds: int):
    """纯函数: 实际成交 vs 回测信号 → (metrics, pairs)。
    actual 项: {dir(buy/sell), ts(epoch), profit, entry(显示串)}
    bt 项:     {dir(BUY/SELL), entry_time(epoch), points, entry(价), exit(价)}
    配对: 每笔实际找最近的未匹配回测笔(entry 差 ≤ 2根bar, 吸收 runner 次开盘偏移)。"""
    n_a, n_b = len(actual), len(bt)
    tol = 2 * tf_seconds
    used, pairs = set(), []
    for a in actual:
        best, bestd = None, tol + 1
        for i, t in enumerate(bt):
            if i not in used and abs(t["entry_time"] - a["ts"]) < bestd:
                best, bestd = i, abs(t["entry_time"] - a["ts"])
        m = bt[best] if (best is not None and bestd <= tol) else None
        if m is not None:
            used.add(best)
        dir_match = m is not None and m["dir"].upper() == a["dir"].upper()
        outcome_match = m is not None and (m["points"] > 0) == (a["profit"] > 0)
        pairs.append({
            "actual": {"entry": a["entry"], "dir": a["dir"], "win": a["profit"] > 0,
                       "profit": round(a["profit"], 2), "price": a.get("price"), "net": a.get("net")},
            "bt": (None if m is None else
                   {"entry": datetime.fromtimestamp(m["entry_time"], tz=timezone.utc).strftime("%m-%d %H:%M"),
                    "dir": m["dir"], "win": m["points"] > 0, "points": m.get("points"),
                    "price": m.get("entry")}),
            "dir_match": dir_match, "outcome_match": outcome_match})
    paired = sum(1 for p in pairs if p["bt"] is not None)
    dir_ok = sum(1 for p in pairs if p["dir_match"])
    outcome_ok = sum(1 for p in pairs if p["outcome_match"])
    union = n_a + n_b - paired                          # 两边并集: 配对 + 实盘多 + 回测多
    count_rate = paired / union if union else 1.0        # 笔数正确率 = 两边都有 / 并集
    dir_rate = dir_ok / union if union else 0.0          # 趋势正确率 = 配对且方向对 / 并集
    outcome_rate = outcome_ok / union if union else 0.0  # 涨跌正确率 = 配对且盈亏方向对 / 并集
    signal_hit = paired / n_a if n_a else 0.0            # 辅助: 实盘有信号回测也有
    metrics = {
        "count_match_rate": round(count_rate, 3),      # 笔数正确率(配对/并集, step1)
        "signal_hit_rate": round(signal_hit, 3),       # 实际有信号回测也有(辅助)
        "dir_match_rate": round(dir_rate, 3),          # 趋势正确率(配对且方向对/并集, step2)
        "outcome_match_rate": round(outcome_rate, 3),  # 涨跌正确率(配对且盈亏对/并集, step2)
        "union": union, "paired": paired,              # 并集/配对数, 供页面显示分母
    }
    metrics["match_score"] = round(100 * (0.4 * signal_hit + 0.2 * count_rate
                                          + 0.2 * dir_rate + 0.2 * outcome_rate), 1)
    # 回测质量v1 达标: 只判 笔数(trade) & 方向(direction) 两率 ≥ 90%(阈值暂写死, 未来进config);
    # 信号(indicator)/涨跌(outcome) 照算照存, 记录/展示用, 不进 v1 考核
    metrics["q10_pass"] = (count_rate >= 0.9 and dir_rate >= 0.9)
    metrics["q10_target"] = 0.9
    # 两边对账: 补"回测有信号、实盘没下单"那一边(能抓 runner 漏单)。不计入上面的率, 仅显示
    for i, t in enumerate(bt):
        if i not in used:
            pairs.append({
                "actual": None, "dir_match": False, "outcome_match": False,
                "bt": {"entry": datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).strftime("%m-%d %H:%M"),
                       "dir": t["dir"], "win": t["points"] > 0, "points": t.get("points"),
                       "price": t.get("entry")}})
    return metrics, pairs


@router.get("/reconcile/{strategy_id}")
async def reconcile(strategy_id: int, request: Request, scope: str = "all"):
    """关2对账: 用该策略实盘/demo成交(scope: all=demo+live)验证回测 —
    自动取实际成交时间窗 → 切片回测同窗 → 4 个一致率 + 综合分, 落 reconciliations(覆盖)。"""
    pool = request.app.state.pool
    strat = await pool.fetchrow("SELECT symbol, timeframe FROM strategies WHERE id=$1", strategy_id)
    if strat is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    q = ("SELECT direction, entry_time, exit_time, profit, entry_price, net_points FROM trades"
         " WHERE strategy_id=$1")
    args = [strategy_id]
    if scope != "all":
        args.append(scope.upper()); q += f" AND env = ${len(args)}"
    q += " ORDER BY entry_time"
    actual = await pool.fetch(q, *args)
    out = {"strategy_id": strategy_id, "scope": scope, "symbol": strat["symbol"],
           "window_from": None, "window_to": None,
           "actual_trades": len(actual), "bt_trades": 0, "metrics": {}}
    if not actual:
        out["note"] = "该策略暂无 demo/live 成交, 无法对账"
        return out
    wf = min(a["entry_time"] for a in actual)
    wt = max(a["exit_time"] for a in actual)
    bt_row = await pool.fetchrow(
        "SELECT trades, from_time, to_time FROM backtests WHERE strategy_id=$1 AND symbol=$2",
        strategy_id, strat["symbol"])
    bt_all = (bt_row["trades"] if bt_row else []) or []
    wf_ts, wt_ts = wf.timestamp(), wt.timestamp()
    bt = [t for t in bt_all if wf_ts <= t["entry_time"] <= wt_ts]  # 切到实际成交窗口
    metrics, pairs = _reconcile_metrics(
        [{"dir": a["direction"], "ts": a["entry_time"].timestamp(), "profit": a["profit"],
          "entry": a["entry_time"].strftime("%m-%d %H:%M"),
          "price": a["entry_price"], "net": a["net_points"]} for a in actual],
        bt, TF_SECONDS.get(strat["timeframe"], 900))
    # 回测覆盖信息: 让人看清"回测总共几笔/覆盖到几号", 分辨低匹配是'回测没覆盖'还是'真没信号'
    bt_to = bt_row["to_time"] if bt_row else None
    if bt_to is not None and bt_to.tzinfo is None:   # naive→aware, 防与 wt(aware)比较 TypeError
        bt_to = bt_to.replace(tzinfo=timezone.utc)
    out.update(window_from=wf, window_to=wt, bt_trades=len(bt), metrics=metrics, pairs=pairs,
               bt_total=len(bt_all),
               bt_from=(bt_row["from_time"] if bt_row else None), bt_to=bt_to,
               # 回测末尾早于 demo 末尾 = 回测没覆盖到近期 → 提示重跑
               bt_stale=(bt_to is not None and bt_to < wt))
    await pool.execute(
        "INSERT INTO reconciliations (strategy_id, scope, window_from, window_to,"
        "   actual_trades, bt_trades, match_score, metrics)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8)"
        " ON CONFLICT (strategy_id, scope) DO UPDATE SET"
        "   window_from=EXCLUDED.window_from, window_to=EXCLUDED.window_to,"
        "   actual_trades=EXCLUDED.actual_trades, bt_trades=EXCLUDED.bt_trades,"
        "   match_score=EXCLUDED.match_score, metrics=EXCLUDED.metrics, updated_at=now()",
        strategy_id, scope, wf, wt, len(actual), len(bt), metrics["match_score"], metrics)
    return out


# ---------- 系统流水: 本地库(trades)已持久化成交, 按账号 + 时间范围查(v1.6) ----------
TEST_MAGIC = 999999  # 下单测试单 magic(bridge ordertest); 默认从流水/核对里过滤


@router.get("/trades/local")
async def trades_local(request: Request, account: Optional[int] = None,
                       from_time: Optional[datetime] = None, to_time: Optional[datetime] = None,
                       include_test: bool = False, limit: int = 1000):
    """读本地库 trades(MT5 已平仓的持久副本): 按 account + 平仓时间范围过滤。
    include_test=false(默认): 过滤掉下单测试单(magic=999999)。
    与 /hosts/{id}/trades(实时拉 MT5)互补 — 这个不限 90 天、离线也能查。"""
    pool = request.app.state.pool
    if from_time and from_time.tzinfo is None:      # naive → 按 UTC(券商时间口径), 与 timestamptz 一致
        from_time = from_time.replace(tzinfo=timezone.utc)
    if to_time and to_time.tzinfo is None:
        to_time = to_time.replace(tzinfo=timezone.utc)
    # 账号带券商(优先库里存的 broker; 老行未回填则回退到 mt5_login→mt5_server), "券商→账号"两级过滤
    accounts = [dict(r) for r in await pool.fetch(
        "SELECT DISTINCT t.account, COALESCE(t.broker, hh.mt5_server) AS broker"
        " FROM trades t LEFT JOIN mt5_hosts hh ON hh.mt5_login = t.account"
        " ORDER BY COALESCE(t.broker, hh.mt5_server) NULLS LAST, t.account")]
    q, args = "SELECT * FROM trades WHERE true", []
    if account:
        args.append(account); q += f" AND account = ${len(args)}"
    if from_time:
        args.append(from_time); q += f" AND exit_time >= ${len(args)}"
    if to_time:
        args.append(to_time); q += f" AND exit_time <= ${len(args)}"
    if not include_test:
        q += f" AND magic <> {TEST_MAGIC}"      # 默认过滤下单测试单
    args.append(limit)
    q += f" ORDER BY exit_time DESC LIMIT ${len(args)}"
    rows = await pool.fetch(q, *args)
    return {"accounts": accounts, "trades": [dict(r) for r in rows]}


@router.get("/trades/consistency")
async def trades_consistency(request: Request, account: int, from_time: datetime,
                             to_time: Optional[datetime] = None, include_test: bool = False):
    """按需一致性核对: 本时间段 库(trades)笔数 vs 该账号 worker 实时 MT5 已平仓笔数。
    两数相等 = 一致(库可信); 不等 = 库疑似漏存/多存。worker 离线则无法实时核对。
    include_test 两边同口径生效(默认都过滤下单测试单) — 单边过滤会造成假不一致。"""
    pool = request.app.state.pool
    # ① 归一时区: web 可能传 naive(datetime.now().isoformat()无tz) → 与 aware 运算会 TypeError。
    #   trades.exit_time 存的是券商时间(按 UTC 标), 故 naive 一律按 UTC 处理。
    if from_time.tzinfo is None:
        from_time = from_time.replace(tzinfo=timezone.utc)
    to_time = to_time or datetime.now(timezone.utc)
    if to_time.tzinfo is None:
        to_time = to_time.replace(tzinfo=timezone.utc)
    db = None
    try:  # ② 兜底: 任何异常都返回优雅结果, 绝不 500(核对是辅助功能, 不该拖垮页面)
        db_q = "SELECT count(*) FROM trades WHERE account=$1 AND exit_time>=$2 AND exit_time<=$3"
        if not include_test:
            db_q += f" AND magic <> {TEST_MAGIC}"
        db = await pool.fetchval(db_q, account, from_time, to_time)
        host = await pool.fetchrow(
            "SELECT host, port FROM mt5_hosts WHERE mt5_login=$1 AND enabled", account)
        if host is None:
            return {"account": account, "db": db, "mt5": None, "consistent": None,
                    "note": "该账号当前无在线 worker 登录, 无法实时核对(库数据仍在)"}
        ft, tt = from_time.timestamp(), to_time.timestamp()
        days = max(1, int((datetime.now(timezone.utc) - from_time).total_seconds() // 86400) + 2)
        headers = ({"X-API-Key": os.getenv("BRIDGE_API_KEY", "")}
                   if os.getenv("BRIDGE_API_KEY") else {})
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(f"http://{host['host']}:{host['port']}/trades",
                                 params={"days": days})
        by_pos: dict = {}
        for d in r.json().get("deals", []):
            pid = d.get("position_id")
            if pid is not None:
                by_pos.setdefault(pid, []).append(d)
        mt5 = 0
        for legs in by_pos.values():
            ins = next((d for d in legs if d.get("entry") == "in"), None)
            out = next((d for d in legs if d.get("entry") == "out"), None)
            if ins and out and ins.get("type") in ("buy", "sell") and ft <= out["time"] <= tt:
                if not include_test and ins.get("magic") == TEST_MAGIC:  # 与库侧同口径过滤
                    continue
                mt5 += 1
        return {"account": account, "db": db, "mt5": mt5, "consistent": db == mt5}
    except Exception as e:
        logger.warning("trades consistency %s failed: %s", account, e)
        return {"account": account, "db": db, "mt5": None, "consistent": None,
                "note": f"核对暂不可用(worker/bridge 不可达或出错): {e}"}


# ---------- 策略分析·维度二实例级(期1): 单策略回测胜负归因 (v1.4) ----------
def _analyze_trades(trades: list, oos: dict, breakdown: list) -> dict:
    """从回测逐笔 + oos + 跨品种行 算单策略胜负归因(纯函数, 读 backtests.trades)。
    trades 项: {dir(BUY/SELL), entry_time(epoch), points, reason, ...}"""
    if not trades:
        return {"has_data": False}
    ts = sorted(trades, key=lambda t: t["entry_time"])
    pts = [t.get("points", 0) for t in ts]
    net = round(sum(pts), 1)
    wins = [p for p in pts if p > 0]
    gwin, gloss = sum(wins), -sum(p for p in pts if p < 0)
    streak = mx = 0                                    # 最长连亏串
    for p in pts:
        streak = streak + 1 if p <= 0 else 0
        mx = max(mx, streak)
    summary = {
        "trades": len(ts), "wins": len(wins),
        "win_rate": round(len(wins) / len(ts), 3),
        "net_points": net,
        "profit_factor": round(gwin / gloss, 2) if gloss else None,
        "max_consec_loss": mx,
        # 单笔最大盈利占净利比: 越高=越靠个别几笔(脆, 拟合的另一种长相)
        "top_trade_pct": round(max(wins) / net, 3) if wins and net > 0 else None,
    }
    direction = {}                                     # 方向不对称
    for d in ("BUY", "SELL"):
        dp = [t.get("points", 0) for t in ts if (t.get("dir") or "").upper() == d]
        direction[d] = {"trades": len(dp), "net": round(sum(dp), 1),
                        "win_rate": round(sum(1 for p in dp if p > 0) / len(dp), 3) if dp else None}
    reasons: dict = {}                                 # 出场原因构成
    for t in ts:
        e = reasons.setdefault(t.get("reason") or "?", {"trades": 0, "net": 0.0})
        e["trades"] += 1
        e["net"] = round(e["net"] + t.get("points", 0), 1)
    by_hour, by_wd = {}, {}                             # 时段 / 星期效应
    for t in ts:
        dt = datetime.fromtimestamp(t["entry_time"], tz=timezone.utc)
        for bucket, key in ((by_hour, dt.hour), (by_wd, dt.weekday())):
            e = bucket.setdefault(key, {"trades": 0, "net": 0.0})
            e["trades"] += 1
            e["net"] = round(e["net"] + t.get("points", 0), 1)
    overfit = {"time": oos or {}, "symbol": [          # 拟合判别: 时间轴(oos) + 品种轴(跨品种)
        {"symbol": b["symbol"], "net": (b["metrics"] or {}).get("net_points"),
         "trades": (b["metrics"] or {}).get("trades")} for b in breakdown]}
    return {"has_data": True, "summary": summary, "direction": direction, "reasons": reasons,
            "by_hour": [{"hour": h, **v} for h, v in sorted(by_hour.items())],
            "by_weekday": [{"wd": w, **v} for w, v in sorted(by_wd.items())],
            "overfit": overfit}


@router.get("/analysis/{strategy_id}")
async def strategy_analysis(strategy_id: int, request: Request, symbol: Optional[str] = None):
    """单策略【回测】胜负归因(维度二期1): 读指定品种(默认主品种) backtests.trades + oos + 跨品种行。
    分析的是【整段回测】(不切窗口)。symbol 可选 → 看该策略在不同货币对上的回测归因。"""
    pool = request.app.state.pool
    strat = await pool.fetchrow(
        "SELECT name, symbol, timeframe FROM strategies WHERE id=$1", strategy_id)
    if strat is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    sym = symbol or strat["symbol"]        # 分析哪个品种的回测(默认主品种)
    all_syms = [r["symbol"] for r in await pool.fetch(   # 该策略回测过的品种(下拉用)
        "SELECT symbol FROM backtests WHERE strategy_id=$1 ORDER BY symbol", strategy_id)]
    main = await pool.fetchrow(
        "SELECT trades, metrics FROM backtests WHERE strategy_id=$1 AND symbol=$2",
        strategy_id, sym)
    breakdown = await pool.fetch(
        "SELECT symbol, metrics FROM backtests WHERE strategy_id=$1 ORDER BY symbol", strategy_id)
    trades = (main["trades"] if main else []) or []
    oos = ((main["metrics"] or {}).get("oos") if main and main["metrics"] else {}) or {}
    out = {"strategy_id": strategy_id, "name": strat["name"], "symbol": sym,
           "main_symbol": strat["symbol"], "symbols": all_syms, "timeframe": strat["timeframe"]}
    out.update(_analyze_trades(trades, oos, [dict(b) for b in breakdown]))
    # 逐笔明细(每笔下单 + 胜负): 按时间排, 上限 1000 防超大回测撑爆前端
    st = sorted(trades, key=lambda t: t["entry_time"])
    out["trades"] = [{
        "entry": datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "exit": datetime.fromtimestamp(t["exit_time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "dir": t.get("dir"), "entry_price": t.get("entry"), "exit_price": t.get("exit"),
        "points": t.get("points"), "reason": t.get("reason"),
    } for t in st[:1000]]
    out["trades_capped"] = len(st) > 1000
    return out
