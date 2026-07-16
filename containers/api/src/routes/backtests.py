"""/backtest — 批量回测的调度与结果查询

职责: 挑选策略批次、按品种分组加载 M1(每品种只加载一次)、调用回测引擎、
     结果入库、排名查询。撮合规则本体在 services/backtest.py。

扩展点: 新增回测指标 = services/backtest.py 的 _metrics() 加字段
       (metrics 是 JSONB, 表结构不用动)。
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import backtest, jobs

logger = logging.getLogger("backtests")
router = APIRouter()

# 全局进度 (单进程内存即可)
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
    """批量回测: 拆成 每策略×品种 的 jobs(schema/020)投递后即返回 —
    api 重启批次不丢(consumer 断点续跑), 进度查表(/backtest/status), 多副本安全(铁律5/6)"""
    pool = request.app.state.pool
    if await jobs.has_active(pool):
        raise HTTPException(status_code=409, detail="backtest already running")
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

    # 成本: 请求值 > config 系统默认 > 代码默认(冻结进 payload, 批次内口径一致)
    cfg = await pool.fetchval("SELECT value FROM config WHERE key='backtest_costs'") or {}
    costs = {
        "slippage_points": req.slippage_points if req.slippage_points is not None
                           else cfg.get("slippage_points", backtest.DEFAULT_SLIPPAGE_POINTS),
        "commission_points": req.commission_points if req.commission_points is not None
                             else cfg.get("commission_points", backtest.DEFAULT_COMMISSION_POINTS),
        "spread_points": req.spread_points if req.spread_points is not None
                         else cfg.get("spread_points"),
    }
    # 展开成 每策略×品种 一个 job: 主品种必测(排名要它); 跨品种再并上全 download 品种
    # (反过拟合空间维度)。时间窗冻结在投递时刻; 品种是否存在等校验留给执行时(错误落在 job 行)
    t_from = req.from_time or datetime(2015, 1, 1, tzinfo=timezone.utc)
    t_to = req.to_time or datetime.now(timezone.utc)
    universe = ([r["symbol"] for r in await pool.fetch("SELECT symbol FROM symbols WHERE download")]
                if req.cross_symbol else [])
    items = [{"strategy_id": s["id"], "name": s["name"], "symbol": sym,
              "from": t_from.isoformat(), "to": t_to.isoformat(), "costs": costs}
             for s in rows for sym in {s["symbol"]} | set(universe)]
    await jobs.submit_batch(pool, items)
    return {"started": True, "total": len(items),
            "cross_symbol": req.cross_symbol, "costs": costs}


@router.get("/backtest/status")
async def status(request: Request):
    """批量回测进度(查 jobs 表聚合; 结构与旧内存版一致, web 零改动)"""
    return await jobs.progress(request.app.state.pool)


@router.post("/backtest/cancel")
async def cancel(request: Request):
    """取消当前批次: 删掉未跑完的 jobs(旧世界"重启api=取消"没有了 — 重启会续跑, 取消要显式)。
    正在跑的那一个 job 会跑完但结果无害(幂等 upsert), 行已删不再计数。"""
    n = await request.app.state.pool.execute(
        "DELETE FROM jobs WHERE kind='backtest' AND status IN ('PENDING','RUNNING')")
    return {"cancelled": int(n.split()[-1])}


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

    # 实盘战绩(demo+live 合并, 来自 strategy_stats)—— 回测列不动, 仅附加实盘对比(胜率/笔数/盈亏)
    page_ids = [d["strategy_id"] for d in page_cands]
    if page_ids:
        st = {s["strategy_id"]: s for s in await pool.fetch(
            "SELECT strategy_id, sum(trades) AS t, sum(wins) AS w, sum(profit) AS p"
            " FROM strategy_stats WHERE strategy_id = ANY($1) GROUP BY strategy_id", page_ids)}
        # 实盘券商: strategy_stats(战绩快照)没存券商, 从 trades 逐笔里取实际成交过的券商
        bk = {r["strategy_id"]: r["brokers"] for r in await pool.fetch(
            "SELECT strategy_id, array_agg(DISTINCT broker)"
            "       FILTER (WHERE broker IS NOT NULL) AS brokers"
            " FROM trades WHERE strategy_id = ANY($1) GROUP BY strategy_id", page_ids)}
        for d in page_cands:
            s = st.get(d["strategy_id"])
            d["actual"] = ({"trades": s["t"], "wins": s["w"], "profit": round(float(s["p"]), 2),
                            "win_rate": round(s["w"] / s["t"], 3) if s["t"] else None,
                            "broker": " / ".join(bk.get(d["strategy_id"]) or []) or None}
                           if s and s["t"] else None)
    return {"results": page_cands, "total": total, "page": page, "page_size": limit}


@router.get("/backtest/results/{strategy_id}")
async def results(strategy_id: int, request: Request):
    """单策略各品种的回测记录(跨品种验证的每品种一行)"""
    rows = await request.app.state.pool.fetch(
        "SELECT id, from_time, to_time, symbol, broker, metrics, created_at FROM backtests"
        " WHERE strategy_id=$1 ORDER BY symbol", strategy_id)
    return {"results": [dict(r) for r in rows]}


# ---------- 关2 对账: 回测 vs 实盘(demo/live)逐笔匹配 (v1.6) ----------
PAIR_TOL_SECONDS = 20 * 60  # 对账配对容差 ±20分钟(2026-07-15 定): 吸收 runner 次开盘偏移/秒级成交延迟


def _merge_windows(windows: list) -> list:
    """[[from_ts, to_ts], ...] 排序 + 合并重叠/相邻 — 避免同一笔回测信号被两个窗争抢"""
    merged = []
    for w in sorted(windows):
        if merged and w[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], w[1])
        else:
            merged.append(list(w))
    return merged


def _reconcile_metrics(actual: list, bt: list, tol: int = PAIR_TOL_SECONDS,
                       one_sided: bool = False):
    """纯函数: 实际成交 vs 回测信号 → (metrics, pairs)。
    actual 项: {dir(buy/sell), ts(epoch), profit, entry(显示串)}
    bt 项:     {dir(BUY/SELL), entry_time(epoch), points, entry(价), exit(价)}
    配对: 每笔实际找最近的未匹配回测笔(entry 差 ≤ tol, 默认±20分钟)。
    one_sided: 无运行区间记录的降级口径 — 回测池只剩实盘附近的信号, 抓不了漏单,
    分母用实盘笔数(并集里回测侧被清空, 再用并集会虚高得没意义)。"""
    n_a, n_b = len(actual), len(bt)
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
                       "profit": round(a["profit"], 2), "price": a.get("price"), "net": a.get("net"),
                       "ts": a["ts"]},  # epoch, 供 reconcile() 判缺口原因后 pop 掉
            "bt": (None if m is None else
                   {"entry": datetime.fromtimestamp(m["entry_time"], tz=timezone.utc).strftime("%m-%d %H:%M"),
                    "dir": m["dir"], "win": m["points"] > 0, "points": m.get("points"),
                    "price": m.get("entry"), "ts": m["entry_time"]}),
            "dir_match": dir_match, "outcome_match": outcome_match})
    paired = sum(1 for p in pairs if p["bt"] is not None)
    dir_ok = sum(1 for p in pairs if p["dir_match"])
    outcome_ok = sum(1 for p in pairs if p["outcome_match"])
    union = n_a + n_b - paired                          # 两边并集: 配对 + 实盘多 + 回测多
    denom = n_a if one_sided else union                 # 分母: 双边=并集, 单边=实盘笔数
    count_rate = paired / denom if denom else 1.0        # 笔数正确率 = 两边都有 / 分母
    dir_rate = dir_ok / denom if denom else 0.0          # 趋势正确率 = 配对且方向对 / 分母
    outcome_rate = outcome_ok / denom if denom else 0.0  # 涨跌正确率 = 配对且盈亏方向对 / 分母
    signal_hit = paired / n_a if n_a else 0.0            # 辅助: 实盘有信号回测也有
    metrics = {
        "count_match_rate": round(count_rate, 3),      # 笔数正确率(配对/分母, step1)
        "signal_hit_rate": round(signal_hit, 3),       # 实际有信号回测也有(辅助)
        "dir_match_rate": round(dir_rate, 3),          # 趋势正确率(配对且方向对/分母, step2)
        "outcome_match_rate": round(outcome_rate, 3),  # 涨跌正确率(配对且盈亏对/分母, step2)
        "union": union, "paired": paired,
        "denominator": denom,                          # 页面显示实际用的分母
        "mode": "one_sided" if one_sided else "segments",  # 对账口径入库, 历史分数不混淆
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
                       "price": t.get("entry"), "ts": t["entry_time"]}})
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
    # 对比窗口(双模式, 2026-07-16 定):
    #   有运行区间(strategy_runtime) → 分段双边: 只比"策略真实在跑"的时段, 段内照样抓漏单
    #   区间没覆盖到的实盘笔(老数据/api停摆) → 逐笔±20分钟单窗兜底(单边, 抓不了漏单)
    # 全部窗口合并重叠后过滤回测池; 左端放宽容差: 回测入场=bar开盘, 实盘成交晚几秒,
    # 否则实盘第一笔对应的回测信号永远被切掉(2026-07-15 实测: #177 07-10 09:00 误报"未触发")
    tol = PAIR_TOL_SECONDS
    segs = await pool.fetch(
        "SELECT run_from, run_to FROM strategy_runtime"
        " WHERE strategy_id=$1 AND run_to >= $2 AND run_from <= $3 ORDER BY run_from",
        strategy_id, wf - timedelta(seconds=tol), wt + timedelta(seconds=tol))
    windows = [[s["run_from"].timestamp() - tol, s["run_to"].timestamp() + tol] for s in segs]
    for a in actual:  # 区间外的实盘笔(成交是事实, 必须参与对账) → 兜底小窗
        a_ts = a["entry_time"].timestamp()
        if not any(w0 <= a_ts <= w1 for w0, w1 in windows):
            windows.append([a_ts - tol, a_ts + tol])
    windows = _merge_windows(windows)
    mode = "segments" if segs else "one_sided"
    bt = [t for t in bt_all if wf_ts - tol <= t["entry_time"] <= wt_ts
          and any(w0 <= t["entry_time"] <= w1 for w0, w1 in windows)]
    metrics, pairs = _reconcile_metrics(
        [{"dir": a["direction"], "ts": a["entry_time"].timestamp(), "profit": a["profit"],
          "entry": a["entry_time"].strftime("%m-%d %H:%M"),
          "price": a["entry_price"], "net": a["net_points"]} for a in actual],
        bt, one_sided=(mode == "one_sided"))
    # 覆盖信息两级: 行情数据(库内M1) / 回测窗口 — 分辨低匹配是'数据没下载'/'回测没跑到'/'真没信号'
    bt_from, bt_to = (bt_row["from_time"], bt_row["to_time"]) if bt_row else (None, None)
    if bt_from is not None and bt_from.tzinfo is None:  # naive→aware, 防与 wt(aware)比较 TypeError
        bt_from = bt_from.replace(tzinfo=timezone.utc)
    if bt_to is not None and bt_to.tzinfo is None:
        bt_to = bt_to.replace(tzinfo=timezone.utc)
    data_to = await pool.fetchval(   # 该品种库内原始 M1 的最新时间(唯一原始数据, 回测的原料)
        "SELECT max(time) FROM historical_bars WHERE symbol=$1 AND timeframe='M1'", strat["symbol"])
    out["broker"] = await pool.fetchval(  # 品种主档的券商 — 补数据提示里点名"下哪家的哪个品种"
        "SELECT broker FROM symbols WHERE symbol=$1", strat["symbol"])
    bt_from_ts = bt_from.timestamp() if bt_from else None
    bt_to_ts = bt_to.timestamp() if bt_to else None
    data_to_ts = data_to.timestamp() if data_to else None
    for p in pairs:  # 每行: ①归属窗口(逐笔对照按窗口分组显示) ②缺口归因(实盘有回测无时)
        ts = (p["actual"] or p["bt"])["ts"]
        p["win"] = next((k for k, (w0, w1) in enumerate(windows) if w0 <= ts <= w1), None)
        if p["bt"] is not None:
            p["bt"].pop("ts", None)
        if p["actual"] is None:
            continue
        p["actual"].pop("ts", None)
        if p["bt"] is not None:
            continue
        if bt_to_ts is not None and (bt_from_ts is None or ts >= bt_from_ts) and ts <= bt_to_ts:
            p["gap"] = "not_triggered"   # 回测已覆盖该时间仍无信号 = 真差异
        elif data_to_ts is not None and ts <= data_to_ts:
            p["gap"] = "bt_stale"        # 库内数据已到, 只是回测没重跑
        else:
            p["gap"] = "data_missing"    # 库内 M1 都没到该时间 → 先下载
    def _fmt(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")
    win_view = [{  # 每段窗口 + 两边笔数(逐笔对照的分组表头); 上限100段防撑爆
        "from": _fmt(w0), "to": _fmt(w1),
        "actual": sum(1 for a in actual if w0 <= a["entry_time"].timestamp() <= w1),
        "bt": sum(1 for t in bt if w0 <= t["entry_time"] <= w1),
    } for w0, w1 in windows[:100]]
    for p in pairs:  # 超出显示上限的窗口归组会丢行 → 防御性归入"窗口外"兜底组
        if p.get("win") is not None and p["win"] >= len(win_view):
            p["win"] = None
    out.update(window_from=wf, window_to=wt, bt_trades=len(bt), metrics=metrics, pairs=pairs,
               bt_total=len(bt_all), bt_from=bt_from, bt_to=bt_to,
               # 对账口径: segments=分段双边(有运行区间) / one_sided=逐笔小窗单边(降级)
               mode=mode, windows=win_view, windows_total=len(windows),
               # 回测末尾早于 demo 末尾 = 回测没覆盖到近期 → 提示重跑
               bt_stale=(bt_to is not None and bt_to < wt),
               # 数据覆盖检查: 库内 M1 是否盖住实盘窗口末尾(✅=不用下载, 重跑回测即可)
               data_to=data_to,
               data_cover=(data_to_ts is not None and data_to_ts >= wt.timestamp()))
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
    # 实盘同款归因(demo/live 成交, trades 表): 与回测归因对照看"回测的赢法实盘还成立吗"。
    # 笔数少时统计意义弱, 页面标注仅供参考; 净点缺失(老数据)时退化用盈亏金额判胜负。
    act_rows = await pool.fetch(
        "SELECT direction, entry_time, profit, commission, swap, net_points, close_reason, env"
        " FROM trades WHERE strategy_id=$1 ORDER BY entry_time", strategy_id)
    act = [{"dir": r["direction"], "entry_time": r["entry_time"].timestamp(),
            "points": (float(r["net_points"]) if r["net_points"] is not None
                       else float(r["profit"])),
            "reason": r["close_reason"] or "?"} for r in act_rows]
    actual = _analyze_trades(act, {}, [])
    actual.pop("overfit", None)            # 实盘无 oos/跨品种概念
    if actual.get("has_data"):
        actual["money"] = round(sum(float(r["profit"]) + float(r["commission"] or 0)
                                    + float(r["swap"] or 0) for r in act_rows), 2)
        actual["envs"] = {e: sum(1 for r in act_rows if r["env"] == e)
                          for e in sorted({r["env"] for r in act_rows if r["env"]})}
    out["actual"] = actual
    return out
