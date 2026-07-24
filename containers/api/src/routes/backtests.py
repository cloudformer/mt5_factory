"""/backtest — 批量回测的调度与结果查询

职责: 挑选策略批次、按品种分组加载 M1(每品种只加载一次)、调用回测引擎、
     结果入库、排名查询。撮合规则本体在 services/backtest.py。

扩展点: 新增回测指标 = services/backtest.py 的 _metrics() 加字段
       (metrics 是 JSONB, 表结构不用动)。
"""
import asyncio
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


def _passes_gate(m: Optional[dict], gate: dict) -> bool:
    """主品种回测成绩过交叉门槛?(config: cross_symbol_gate)
    无主品种成绩 = 不过(先跑出成绩, 下一批自然够格); 门槛项为 null = 不检查;
    PF 为 None(零亏损=∞)视为通过。"""
    if not m or not m.get("trades"):
        return False
    if gate.get("min_trades") is not None and m["trades"] < gate["min_trades"]:
        return False
    if gate.get("min_win_rate") is not None and (m.get("win_rate") or 0) < gate["min_win_rate"]:
        return False
    if gate.get("min_net_points") is not None and (m.get("net_points") or 0) <= gate["min_net_points"]:
        return False
    if gate.get("min_pf") is not None:
        pf = m.get("profit_factor")
        if pf is not None and pf < gate["min_pf"]:
            return False
    if gate.get("max_dd_points") is not None and (m.get("max_dd_points") or 0) > gate["max_dd_points"]:
        return False
    return True


EST_CROSS_PASS_PCT = 20  # 预览用: 未测过的策略预估有 ~20% 出成绩后能过门槛(仅显示, 不影响执行)


async def _cross_qualified(pool, strategy_ids: list[int]) -> tuple[set[int], int]:
    """批量交叉的够格判定 → (够格集合, 未测过主品种的数量)。
    门槛全空(每项都 null) = 完全没配置 = 全部够格(含还没测过主品种的, 与老行为一致)"""
    gate = await pool.fetchval("SELECT value FROM config WHERE key='cross_symbol_gate'") or {}
    if not any(v is not None for v in gate.values()):
        return set(strategy_ids), 0
    mains = {r["strategy_id"]: r["metrics"] for r in await pool.fetch(
        "SELECT b.strategy_id, b.metrics FROM backtests b"
        " JOIN strategies s ON s.id = b.strategy_id AND s.symbol = b.symbol"
        " WHERE b.strategy_id = ANY($1)", strategy_ids)}
    qualified = {sid for sid in strategy_ids if _passes_gate(mains.get(sid), gate)}
    untested = sum(1 for sid in strategy_ids if sid not in mains)
    return qualified, untested


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
    universe = (set(r["symbol"] for r in
                    await pool.fetch("SELECT symbol FROM symbols WHERE download"))
                if req.cross_symbol else set())
    # 交叉门槛(config: cross_symbol_gate): 批量模式只给主品种够格的策略展开交叉;
    # 按 ID 点名 = 点名即信任, 不走门槛随便交叉
    qualified = None
    if req.cross_symbol and not req.strategy_ids:
        qualified, _ = await _cross_qualified(pool, [s["id"] for s in rows])
    items = [{"strategy_id": s["id"], "name": s["name"], "symbol": sym,
              "from": t_from.isoformat(), "to": t_to.isoformat(), "costs": costs}
             for s in rows
             for sym in ({s["symbol"]} | universe
                         if (qualified is None or s["id"] in qualified) else {s["symbol"]})]
    await jobs.submit_batch(pool, items)
    return {"started": True, "total": len(items), "cross_symbol": req.cross_symbol,
            "cross_qualified": (len(qualified) if qualified is not None else None),
            "cross_gated_out": (len(rows) - len(qualified) if qualified is not None else 0),
            "costs": costs}


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
    """运行预览: 按当前选择数一数会跑多少 — N 个策略 × 品种 = K 次(启动前所见即所得)。
    勾交叉且非点名时按 cross_symbol_gate 预演门槛: 够格 q 个展开交叉, 其余只跑主品种。"""
    pool = request.app.state.pool
    limit = await _batch_limit(pool, limit)
    sel = "SELECT id, symbol FROM strategies"
    if strategy_ids is not None:
        try:
            ids = [int(s) for s in strategy_ids.split(",") if s.strip()]
        except ValueError:
            return {"strategies": 0, "symbols_per": 1, "runs": 0}
        if not ids:
            return {"strategies": 0, "symbols_per": 1, "runs": 0}
        rows = await pool.fetch(f"{sel} WHERE id = ANY($1) LIMIT $2", ids, limit)
    else:
        q = f"{sel} WHERE symbol IN (SELECT symbol FROM symbols)"
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
        args.append(limit)
        rows = await pool.fetch(f"{q} LIMIT ${len(args)}", *args)
    n = len(rows)
    universe = set(r["symbol"] for r in
                   await pool.fetch("SELECT symbol FROM symbols WHERE download"))
    if not cross_symbol:
        return {"strategies": n, "symbols_per": 1, "runs": n}
    qualified, untested = (({r["id"] for r in rows}, 0) if strategy_ids is not None  # 点名不设门槛
                           else await _cross_qualified(pool, [r["id"] for r in rows]))
    runs = sum(len({r["symbol"]} | universe) if r["id"] in qualified else 1 for r in rows)
    # 未测过的策略这批出成绩后, 下一批预估 ~20% 过门槛 → 预估新增交叉次数(仅显示, 不影响本批)
    est_next = round(untested * EST_CROSS_PASS_PCT / 100) * max(len(universe) - 1, 1)
    return {"strategies": n, "symbols_per": len(universe), "runs": runs,
            "cross_qualified": len(qualified), "cross_gated_out": n - len(qualified),
            "cross_untested": untested, "est_next_cross": est_next,
            "est_pass_pct": EST_CROSS_PASS_PCT}


# 排名百分位打分(原 _pct_scores/_apply_template_scores)已下推为 top() 里的 SQL 窗口函数(v1.3):
# 语义不变 — None=没证据0分 / 全集单值100 / 并列取平均名次 / 四维加权 round(,1)。
# 2026-07-19 用 60 合成策略临时表对拍, 新旧分数逐一相等后删除 Python 版(git 历史留底)。


@router.get("/backtest/top")
async def top(request: Request, symbol: Optional[str] = None, broker: Optional[str] = None,
              min_trades: int = 0, min_actual_trades: int = 0,
              limit: int = 20, status: Optional[str] = None,
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
    # ---- 过滤条件(所有路径共用) ----
    conds = ["(b.id IS NOT NULL AND (b.metrics->>'trades')::int >= $1"
             " OR b.id IS NULL AND $1 <= 0)"]
    args: list = [min_trades]  # 未回测: min_trades=0 时显示(成绩为空), >0 时不通过(没证据)

    def _and(cond: str, val) -> None:
        args.append(val)
        conds.append(cond.replace("{n}", str(len(args))))

    if symbol:
        _and("s.symbol = ${n}", symbol)
    if template:
        _and("s.template = ${n}", template)
    if broker:
        _and("COALESCE(b.broker, sy.broker) = ${n}", broker)
    if status:
        _and("s.status = ${n}", status.upper())
    if min_win_rate:  # 前端传百分数, metrics 存 0~1
        _and("b.id IS NOT NULL AND COALESCE((b.metrics->>'win_rate')::float, 0) >= ${n}",
             min_win_rate / 100)
    if min_pf:  # PF=null 表示无亏损(毛损为0) → 视为无穷大, 通过; 未回测不通过
        _and("b.id IS NOT NULL AND COALESCE((b.metrics->>'profit_factor')::float, 1e9) >= ${n}",
             min_pf)
    if max_dd is not None:
        _and("b.id IS NOT NULL AND COALESCE((b.metrics->>'max_dd_points')::float, 0) <= ${n}",
             max_dd)
    if min_actual_trades > 0:  # 实盘笔数≥(demo+live 合计, strategy_stats); 没实盘成交的不通过
        _and("(SELECT COALESCE(sum(st.trades), 0) FROM strategy_stats st"
             " WHERE st.strategy_id = s.id) >= ${n}", min_actual_trades)
    if positive_only:
        conds.append("(b.metrics->>'net_points')::float > 0")
    if oos_pass:  # 留出段一票否决: 留出净点>0 且有交易才通过(老结果没有oos字段=不通过)
        conds.append("COALESCE((b.metrics#>>'{oos,holdout,net_points}')::float, 0) > 0"
                     " AND COALESCE((b.metrics#>>'{oos,holdout,trades}')::int, 0) > 0")
    # 服务端搜索: 只有策略名模糊, 其余精准
    if q_text and q_text.strip() and q_field:
        t = q_text.strip()
        if q_field == "name":
            _and("s.name ILIKE ${n}", f"%{t}%")
        elif q_field == "id" and t.isdigit():
            _and("s.id = ${n}", int(t))
        elif q_field == "timeframe":
            _and("s.timeframe = ${n}", t.upper())
        elif q_field == "status":
            _and("s.status = ${n}", t.upper())

    tpl = None
    if rank_template:  # 排名模板不存在 → 回落默认净点排序(与旧行为一致)
        tpl = next((t for t in (await pool.fetchval(
            "SELECT value FROM config WHERE key='ranking_templates'") or [])
            if t.get("name") == rank_template), None)

    cols = ("s.id AS strategy_id, s.name, s.template, s.symbol, s.timeframe, s.status,"
            " s.params, s.magic_number, s.volume,"
            " COALESCE(b.broker, sy.broker) AS broker, b.metrics, b.created_at")
    joins = (" FROM strategies s"
             " LEFT JOIN backtests b ON b.strategy_id = s.id AND b.symbol = s.symbol"
             " LEFT JOIN symbols sy ON sy.symbol = s.symbol")
    lo = max(page - 1, 0) * limit

    # ---- v1.3: 排序/分页/评分全下推 SQL, 只搬回当页行; total 用窗口 count(*) ----
    if tpl is None and min_robust is None:
        # 默认净点排序: 未回测(NULL)沉底, s.id 定序保证翻页稳定
        args += [limit, lo]
        rows = await pool.fetch(
            f"SELECT {cols}, count(*) OVER () AS _total{joins} WHERE {' AND '.join(conds)}"
            f" ORDER BY (b.metrics->>'net_points')::float DESC NULLS LAST, s.id"
            f" LIMIT ${len(args) - 1} OFFSET ${len(args)}", *args)
        score_by_id: dict = {}
    else:
        # 健壮性聚合(跨品种: 有交易的品种数/其中盈利数) — min_robust 过滤与 robust 维度共用
        joins += (" LEFT JOIN LATERAL ("
                  "SELECT count(*) FILTER (WHERE (bb.metrics->>'trades')::int > 0) AS tested,"
                  "       count(*) FILTER (WHERE (bb.metrics->>'trades')::int > 0"
                  "         AND (bb.metrics->>'net_points')::float > 0) AS profitable"
                  " FROM backtests bb WHERE bb.strategy_id = s.id) rb ON true")
        if min_robust is not None:  # 健壮性≥: 没跑跨品种/没交易 → 不通过
            _and("rb.tested > 0 AND rb.profitable * 100.0 / rb.tested >= ${n}", min_robust)
        where = " AND ".join(conds)
        if tpl is None:  # 只过滤健壮性, 仍按净点排序
            args += [limit, lo]
            rows = await pool.fetch(
                f"SELECT {cols}, count(*) OVER () AS _total{joins} WHERE {where}"
                f" ORDER BY (b.metrics->>'net_points')::float DESC NULLS LAST, s.id"
                f" LIMIT ${len(args) - 1} OFFSET ${len(args)}", *args)
            score_by_id = {}
        else:
            # 排名模板四维百分位加权(0~100), 窗口函数复刻 _pct_scores 语义:
            #   无证据(NULL)=0分不参与排名; 全集只有1个有值=100; 并列段取平均名次。
            #   排序键 = round(score,1) 再净点 — 与旧 Python 排序完全同口径。
            def _pct_sql(v: str) -> str:
                return (f"CASE WHEN {v} IS NULL THEN 0"
                        f" WHEN count({v}) OVER () = 1 THEN 100"
                        f" ELSE (2.0 * (rank() OVER (ORDER BY {v} NULLS LAST) - 1)"
                        f"       + count(*) OVER (PARTITION BY {v}) - 1)"
                        f"      / 2.0 / (count({v}) OVER () - 1) * 100 END")
            w = {k: float(tpl.get(k, 0) or 0) for k in ("stable", "profit", "risk", "robust")}
            args += [w["stable"], w["profit"], w["risk"], w["robust"],
                     sum(w.values()) or 1.0, limit, lo]
            k = len(args)
            score_expr = (f"round(((p_stable * ${k - 6} + p_profit * ${k - 5}"
                          f" + p_risk * ${k - 4} + p_robust * ${k - 3}) / ${k - 2})::numeric, 1)")
            rows = await pool.fetch(
                f"WITH cand AS (SELECT {cols}, rb.tested, rb.profitable,"
                f"  CASE WHEN b.id IS NULL THEN NULL"
                f"       WHEN b.metrics->>'profit_factor' IS NULL THEN 'Infinity'::float"
                f"       ELSE (b.metrics->>'profit_factor')::float END AS v_stable,"
                f"  CASE WHEN b.id IS NULL THEN NULL"
                f"       ELSE COALESCE((b.metrics->>'net_points')::float, 0) END AS v_profit,"
                f"  CASE WHEN b.id IS NULL THEN NULL"
                f"       ELSE -COALESCE((b.metrics->>'max_dd_points')::float, 0) END AS v_risk,"
                f"  CASE WHEN rb.tested > 0 THEN rb.profitable::float / rb.tested"
                f"       ELSE NULL END AS v_robust"
                f" {joins} WHERE {where}),"
                f" pct AS (SELECT *, {_pct_sql('v_stable')} AS p_stable,"
                f"  {_pct_sql('v_profit')} AS p_profit, {_pct_sql('v_risk')} AS p_risk,"
                f"  {_pct_sql('v_robust')} AS p_robust FROM cand)"
                f" SELECT *, {score_expr} AS score, count(*) OVER () AS _total FROM pct"
                f" ORDER BY {score_expr} DESC,"
                f"  (metrics->>'net_points')::float DESC NULLS LAST, strategy_id"
                f" LIMIT ${k - 1} OFFSET ${k}", *args)
            score_by_id = {r["strategy_id"]: float(r["score"]) for r in rows}
    total = rows[0]["_total"] if rows else 0

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

    # 排序/过滤/评分已全部在 SQL 完成 — 这里只为【当页】拉跨品种明细并组装(载荷随页大小)
    bd_map = await _breakdown([r["strategy_id"] for r in rows])
    page_cands = []
    for r in rows:
        d = _build(r, bd_map)   # breakdown/tested/profitable 统一由明细口径重算
        for aux in ("_total", "v_stable", "v_profit", "v_risk", "v_robust",
                    "p_stable", "p_profit", "p_risk", "p_robust"):
            d.pop(aux, None)
        d["score"] = score_by_id.get(d["strategy_id"])
        page_cands.append(d)

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
# 对账配对容差默认值(分钟): 实际值读 config recon_pair_tol_minutes(配置页"回测与实盘时间
# 窗口差距"可随时调)。实测成交滞后仅 3~8 秒, 2 分钟余量足且<最小周期M5的一半; runner 错过收盘晚一根
# bar 补单(M15=15分钟)将配不上 → 如实暴露为执行差异, 不宽恕。
DEFAULT_PAIR_TOL_MINUTES = 2
PAIR_TOL_SECONDS = DEFAULT_PAIR_TOL_MINUTES * 60  # 兜底(config 缺失时)


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
                       "profit": round(a["profit"], 2), "price": a.get("price"),
                       "exit_price": a.get("exit_price"), "net": a.get("net"),
                       "ts": a["ts"]},  # epoch, 供 reconcile() 判缺口原因后 pop 掉
            "bt": (None if m is None else
                   {"entry": datetime.fromtimestamp(m["entry_time"], tz=timezone.utc).strftime("%m-%d %H:%M"),
                    "dir": m["dir"], "win": m["points"] > 0, "points": m.get("points"),
                    "price": m.get("entry"), "exit_price": m.get("exit"), "ts": m["entry_time"]}),
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
    # 精度偏差(记录不评判, AI 校准用): 配对笔的 入场价均差 / 净点均差 / 总账偏差%
    md = [p for p in pairs if p["bt"] is not None
          and p["actual"].get("price") is not None and p["bt"].get("price") is not None]
    if md:
        metrics["entry_diff_avg"] = round(
            sum(abs(float(p["actual"]["price"]) - float(p["bt"]["price"])) for p in md) / len(md), 5)
        # 买/卖分开的带符号入场均差(正=实盘吃亏=回测入场偏乐观): 买=实盘-回测, 卖=回测-实盘。
        # 库内bar是bid价: 买按ask成交(偏差≈点差+滑点), 卖按bid(≈纯滑点) → 两组分开可拆出
        # 点差 vs 滑点, 攒够样本后精准校准成本模型(v1.2)
        for d, key in (("buy", "entry_diff_buy"), ("sell", "entry_diff_sell")):
            grp = [p for p in md if (p["actual"].get("dir") or "").lower() == d]
            if grp:
                sgn = 1 if d == "buy" else -1
                metrics[key] = round(sum(sgn * (float(p["actual"]["price"]) - float(p["bt"]["price"]))
                                         for p in grp) / len(grp), 5)
                metrics[key + "_n"] = len(grp)
    mn = [p for p in pairs if p["bt"] is not None
          and p["actual"].get("net") is not None and p["bt"].get("points") is not None]
    if mn:
        metrics["net_diff_avg"] = round(
            sum(abs(float(p["actual"]["net"]) - float(p["bt"]["points"])) for p in mn) / len(mn), 1)
        sum_a = sum(float(p["actual"]["net"]) for p in mn)
        sum_b = sum(float(p["bt"]["points"]) for p in mn)
        metrics["net_bias_pct"] = (round((sum_b - sum_a) / abs(sum_a) * 100, 2)
                                   if sum_a else None)  # 正=回测偏乐观
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
    """关2对账端点 — 计算逻辑在 compute_reconcile(策略分析页与 AI 成绩单共用)"""
    return await compute_reconcile(request.app.state.pool, strategy_id, scope)


async def compute_reconcile(pool, strategy_id: int, scope: str = "all") -> dict:
    """关2对账: 用该策略实盘/demo成交(scope: all=demo+live)验证回测 —
    自动取实际成交时间窗 → 切片回测同窗 → 4 个一致率 + 综合分, 落 reconciliations(覆盖)。"""
    strat = await pool.fetchrow(
        "SELECT s.symbol, s.timeframe, s.template, s.params, sym.point FROM strategies s"
        " LEFT JOIN symbols sym ON sym.symbol = s.symbol WHERE s.id=$1", strategy_id)
    if strat is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    q = ("SELECT direction, entry_time, exit_time, profit, entry_price, exit_price, net_points"
         " FROM trades WHERE strategy_id=$1")
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
    wf_ts, wt_ts = wf.timestamp(), wt.timestamp()
    # 对比窗口(双模式, 2026-07-16 定):
    #   有运行区间(strategy_runtime) → 分段双边: 只比"策略真实在跑"的时段, 段内照样抓漏单
    #   区间没覆盖到的实盘笔(老数据/api停摆) → 逐笔±20分钟单窗兜底(单边, 抓不了漏单)
    # 全部窗口合并重叠后过滤回测池; 左端放宽容差: 回测入场=bar开盘, 实盘成交晚几秒,
    # 否则实盘第一笔对应的回测信号永远被切掉(2026-07-15 实测: #177 07-10 09:00 误报"未触发")
    tol_min = await pool.fetchval(
        "SELECT value FROM config WHERE key='recon_pair_tol_minutes'")
    tol = int(tol_min or DEFAULT_PAIR_TOL_MINUTES) * 60
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
    # 对账重放(2026-07-23 定): 比对不用库里全量回测的成交切片, 而是"从实盘上线时刻空仓起跑"
    # 内存现算一遍 — 起点状态对齐。全量回测是连续模拟, 进窗口时可能正持着仓, 单仓互斥会
    # 级联堵信号(#3533 实测 8 笔错 6 笔全是这种假差异)。重放不落库; 排名/成绩/回测分析
    # 继续用全量回测(backtests 表), 两个口径各答各的问题。附带红利: 重放永远新鲜,
    # "回测过期(bt_stale)"这一类缺口从此不存在。
    cfg = await pool.fetchval("SELECT value FROM config WHERE key='backtest_costs'") or {}
    costs = {"slippage_points": cfg.get("slippage_points", backtest.DEFAULT_SLIPPAGE_POINTS),
             "commission_points": cfg.get("commission_points", backtest.DEFAULT_COMMISSION_POINTS),
             "spread_points": cfg.get("spread_points")}
    bt_all, replay_to_ts = [], None
    if strat["point"] and strat["timeframe"] in backtest.TF_SECONDS:
        try:
            tf_sec = backtest.TF_SECONDS[strat["timeframe"]]
            warm = backtest.make_strategy(
                strat["template"], strat["params"], strat["point"]).warmup
            lead = warm * tf_sec * 3 + 2 * 86400   # 热身3倍余量+2天: 周末/缺分钟也够
            m1 = await backtest.load_m1(
                pool, strat["symbol"],
                datetime.fromtimestamp(wf_ts - tol - lead, tz=timezone.utc),
                datetime.fromtimestamp(wt_ts, tz=timezone.utc) + timedelta(days=5))
            if m1 is not None and len(m1["time"]):
                res = await asyncio.to_thread(
                    backtest.run_backtest, m1, strat["template"], strat["params"],
                    strat["point"], strat["timeframe"], oos_split=None,
                    start_ts=int(wf_ts - tol), **costs)  # 左端同容差放宽, 首笔信号不被切
                bt_all = res["trades"]
                replay_to_ts = int(m1["time"][-1])
        except Exception as e:  # 重放失败不挡对账页(降级为"回测侧无数据")
            logger.warning("reconcile replay failed for #%s: %s", strategy_id, e)
    bt = [t for t in bt_all if wf_ts - tol <= t["entry_time"] <= wt_ts
          and any(w0 <= t["entry_time"] <= w1 for w0, w1 in windows)]
    metrics, pairs = _reconcile_metrics(
        [{"dir": a["direction"], "ts": a["entry_time"].timestamp(), "profit": a["profit"],
          "entry": a["entry_time"].strftime("%m-%d %H:%M"),
          "price": a["entry_price"], "exit_price": a["exit_price"], "net": a["net_points"]}
         for a in actual],
        bt, tol=tol, one_sided=(mode == "one_sided"))
    # 覆盖信息: 回测侧=重放实际吃到的范围(重放现算, 永远新鲜; 只剩'数据没下载'/'真没信号'两类)
    bt_from = datetime.fromtimestamp(wf_ts - tol, tz=timezone.utc) if replay_to_ts else None
    bt_to = datetime.fromtimestamp(replay_to_ts, tz=timezone.utc) if replay_to_ts else None
    data_to = await pool.fetchval(   # 该品种库内原始 M1 的最新时间(唯一原始数据, 回测的原料)
        "SELECT max(time) FROM historical_bars WHERE symbol=$1 AND timeframe='M1'", strat["symbol"])
    out["broker"] = await pool.fetchval(  # 品种主档的券商 — 补数据提示里点名"下哪家的哪个品种"
        "SELECT broker FROM symbols WHERE symbol=$1", strat["symbol"])
    data_to_ts = data_to.timestamp() if data_to else None
    point = float(strat["point"]) if strat["point"] else None
    for p in pairs:  # 配对行补每笔差值(页面详情显示): 入场/出场价差(点+%), 净点差(点+%)
        if p["actual"] is not None and p["bt"] is not None:
            if point and p["actual"].get("price") is not None and p["bt"].get("price") is not None:
                a_pr, b_pr = float(p["actual"]["price"]), float(p["bt"]["price"])
                p["entry_diff_points"] = round((a_pr - b_pr) / point)
                p["entry_diff_pct"] = round((a_pr - b_pr) / b_pr * 100, 2)
            if point and p["actual"].get("exit_price") is not None and p["bt"].get("exit_price") is not None:
                a_ex, b_ex = float(p["actual"]["exit_price"]), float(p["bt"]["exit_price"])
                p["exit_diff_points"] = round((a_ex - b_ex) / point)
                p["exit_diff_pct"] = round((a_ex - b_ex) / b_ex * 100, 2)
            if p["actual"].get("net") is not None and p["bt"].get("points"):
                a_net, b_net = float(p["actual"]["net"]), float(p["bt"]["points"])
                p["net_diff_points"] = round(a_net - b_net)               # 净点本就是点, 直接相减
                p["net_diff_pct"] = round((a_net - b_net) / abs(b_net) * 100, 2)
    for p in pairs:  # 每行: ①归属窗口(逐笔对照按窗口分组显示) ②缺口归因(实盘有回测无时)
        ts = (p["actual"] or p["bt"])["ts"]
        p["window"] = next((k for k, (w0, w1) in enumerate(windows) if w0 <= ts <= w1), None)
        if p["bt"] is not None:
            p["bt"].pop("ts", None)
        if p["actual"] is None:
            continue
        p["actual"].pop("ts", None)
        if p["bt"] is not None:
            continue
        if replay_to_ts is not None and ts <= replay_to_ts:
            p["gap"] = "not_triggered"   # 重放已覆盖该时间仍无信号 = 真差异
        else:
            p["gap"] = "data_missing"    # 库内 M1 未到该时间 → 先下载(重放现算, 无"回测过期")
    def _fmt(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")
    win_view = [{  # 每段窗口 + 两边笔数(逐笔对照的分组表头); 上限100段防撑爆
        "from": _fmt(w0), "to": _fmt(w1),
        "actual": sum(1 for a in actual if w0 <= a["entry_time"].timestamp() <= w1),
        "bt": sum(1 for t in bt if w0 <= t["entry_time"] <= w1),
    } for w0, w1 in windows[:100]]
    for p in pairs:  # 超出显示上限的窗口归组会丢行 → 防御性归入"窗口外"兜底组
        if p.get("window") is not None and p["window"] >= len(win_view):
            p["window"] = None
    out.update(window_from=wf, window_to=wt, bt_trades=len(bt), metrics=metrics, pairs=pairs,
               bt_total=len(bt_all), bt_from=bt_from, bt_to=bt_to,
               tol_minutes=tol // 60,   # 页面文案用, 调容差不用改模板
               # 对账口径: segments=分段双边(有运行区间) / one_sided=逐笔小窗单边(降级)
               mode=mode, windows=win_view, windows_total=len(windows),
               # 有成交的窗口数(徽章"5段(4活跃·1静默)"用, 消除与两边笔数对不上的歧义)
               windows_active=sum(1 for w in win_view if w["actual"] or w["bt"]),
               # 重放现算永远新鲜, 不存在"回测过期"(键保留=False, web 老模板兼容)
               bt_stale=False,
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
    # 实盘同款归因: 与回测归因对照看"回测的赢法实盘还成立吗"(共用函数, AI成绩单也用它)
    out["actual"] = await actual_attribution(pool, strategy_id)
    return out


async def actual_attribution(pool, strategy_id: int) -> dict:
    """实盘(trades 表)胜负归因 — 策略分析页与 AI 成绩单共用。
    净点缺失(老数据)时退化用盈亏金额判胜负; 笔数少统计意义弱, 由调用方标注。"""
    act_rows = await pool.fetch(
        "SELECT direction, entry_time, exit_time, entry_price, exit_price,"
        "       profit, commission, swap, net_points, close_reason, env"
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
        # 逐笔明细(实盘): 与回测逐笔同款字段 + env 区分 demo/live — 前端复用 trade_table 宏
        actual["trades"] = [{
            "entry": r["entry_time"].strftime("%Y-%m-%d %H:%M"),
            "exit": r["exit_time"].strftime("%Y-%m-%d %H:%M") if r["exit_time"] else "—",
            "dir": r["direction"],
            "entry_price": float(r["entry_price"]) if r["entry_price"] is not None else None,
            "exit_price": float(r["exit_price"]) if r["exit_price"] is not None else None,
            "points": (float(r["net_points"]) if r["net_points"] is not None
                       else float(r["profit"])),
            "reason": r["close_reason"] or "—", "env": r["env"],
        } for r in act_rows]
    return actual
