"""v0.6 OOS 筛选 v2(Regime OOS Screen v2) — 自动初筛器, 测试性质可插拔功能。

每个策略【现跑】一次 20 年回测(窗口 = 各段最早起点自动取最大, 与批量回测同一配方:
load_m1 + 悲观撮合 + 成本/oos/trail 同一 config 来源, 结果 UPSERT 回流 backtests),
再按锚点(跑批当天 UTC 0点)把同一份 trades 切三期六段(训练/测试), 六段 PF 全合格 = PASS。
判定逻辑不在本文件 — 全系统一份在 services/oos_v2.py。

两条路(照 v0.5 的定版):
  · 点名诊断(传 ids) = 本请求内同步现跑现判, 只读不入库(任意状态可点名, 强制预览) — 本步已通
  · 全池清理(不传 ids) = 投 jobs 队列(kind=oos_v2) → worker 并行 → 主节点收尾落报告(第4步接)
自包含: 判据参数读写(config oos_v2)在本文件; 设计与移除手册见 docs/2.regime_dirction/v0.6。
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import backtest, identity, jobs, oos_v2, regime

logger = logging.getLogger(__name__)
router = APIRouter()

TAG = oos_v2.TAG   # basis 标签词根(唯一定义在 services/oos_v2.py)

# 报告明细可排序列(白名单, 值=JSONB 表达式): 排序在库里做, 覆盖全量而非当页。
# PF 的 ∞(有笔无亏损, 存 null)按最大值参与排序; 0 笔段/跳过行 NULL 恒沉底
SORTS = {
    "id": "(e->>'id')::bigint",
    "symbol": "e->>'symbol'",
    "name": "e->>'name'",
    "trades": "(e->'total'->>'n')::numeric",
    "net": "(e->'total'->>'net')::numeric",
    "pf": ("CASE WHEN (e->'total'->>'n')::int > 0 AND e->'total'->>'pf' IS NULL"
           " THEN 1e18 ELSE (e->'total'->>'pf')::numeric END"),
    "verdict": "CASE e->>'verdict' WHEN 'pass' THEN 0 WHEN 'fail' THEN 1 ELSE 2 END",
}


def _sort_expr(sort: str) -> Optional[str]:
    """排序键 → SQL 表达式。段 PF 列: seg<期序号>:<train|test>(段是配置数组, 序号动态);
    正则钉死形状 + 序号转 int, 不存在拼接注入面"""
    m = re.fullmatch(r"seg(\d):(train|test)", sort)
    if m:
        base = f"e->'periods'->{int(m.group(1))}->'{m.group(2)}'"
        return (f"CASE WHEN ({base}->>'n')::int > 0 AND {base}->>'pf' IS NULL"
                f" THEN 1e18 ELSE ({base}->>'pf')::numeric END")
    return SORTS.get(sort)

# 点名诊断进度(api 内存, 几个 ID 秒级; 全池清理的队列进度第4步接)
_progress = {"running": False, "done": 0, "total": 0, "current": "", "report_id": None}


# ---------- 判据参数(config oos_v2 唯一源; 校验在 services/oos_v2.cfg_params) ----------
@router.get("/oos_v2/params")
async def oos_params(request: Request):
    """判据 + 本次实际日期预览: 页面配置区直接渲染(锚点=今天, 只是预览, 运行时各批自冻结)"""
    cfg = await request.app.state.pool.fetchval(
        "SELECT value FROM config WHERE key='oos_v2'") or {}
    p = oos_v2.cfg_params(cfg)
    anchor = datetime.now(timezone.utc).date()
    a = oos_v2.anchor_dt(anchor)
    for s in p["segments"]:   # 每段附实际日期(页面"本次实际日期"列, 改配置立刻反映)
        for part in ("train", "test"):
            t0, t1 = oos_v2.seg_window(a, s[part])
            s[f"{part}_dates"] = [
                f"{datetime.fromtimestamp(t0, tz=timezone.utc):%Y-%m-%d}",
                f"{datetime.fromtimestamp(t1, tz=timezone.utc):%Y-%m-%d}"]
    return {**p, "anchor": anchor.isoformat(), "window_years": oos_v2.window_years(p)}


@router.put("/oos_v2/params")
async def oos_params_save(request: Request):
    """保存判据(整包): 校验不过 = 400 不落库(config 唯一源, 页面是唯一编辑处)"""
    body = await request.json()
    try:
        p = oos_v2.cfg_params(body)   # 非法配置在这里被拒
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await request.app.state.pool.execute(
        "INSERT INTO config (key, value) VALUES ('oos_v2', $1)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", p)
    return p


# ---------- 范围(plan 与 run 共用; 默认 = 全部未筛过的空闲策略, ID 点名 = 只读诊断) ----------
def _scope_conds(req_ids, uid):
    if req_ids:
        conds, args = [], [req_ids]
        conds.append(f"s.id = ANY(${len(args)})")
        scope = {"ids": req_ids}
    else:
        # 已筛过 = tags 里有本模块履历(schema/064) 或 老格式 basis 标签(存量不追改)
        conds, args = ["s.status = 'CANDIDATE'",
                       f"s.tags::text NOT LIKE '%{TAG}#%'",
                       f"COALESCE(s.basis, '') NOT LIKE '%{TAG}#%'"], []
        scope = {"pool": "unscreened"}
    if uid:
        args.append(uid)
        conds.append(f"s.owner_id = ${len(args)}")
    return conds, args, scope


@router.get("/oos_v2/plan")
async def oos_plan(request: Request, ids: Optional[str] = None):
    """运行预估(页面预览行实时刷): 匹配多少策略 / 涉及哪些品种 — 纯读零动作。
    不做 M1 覆盖预检: 数据不够的段自然 0 笔(无数据不追责算过+警示), 代码不设跳开逻辑。"""
    pool = request.app.state.pool
    id_list = None
    if ids:
        try:
            id_list = [int(x) for x in ids.replace("，", ",").split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="ID 列表需为逗号分隔的整数")
    p = oos_v2.cfg_params(
        await pool.fetchval("SELECT value FROM config WHERE key='oos_v2'") or {})
    conds, args, _ = _scope_conds(id_list, identity.scope_uid(request))
    row = await pool.fetchrow(
        "SELECT count(*)::int AS total,"
        f"      count(*) FILTER (WHERE s.tags::text LIKE '%{TAG}#%'"
        f"                        OR COALESCE(s.basis, '') LIKE '%{TAG}#%')::int AS tagged,"
        "       count(DISTINCT s.symbol)::int AS symbols"
        f" FROM strategies s WHERE {' AND '.join(conds)}", *args)
    anchor = datetime.now(timezone.utc).date()
    # 可复用读数(纯显示): 全局配置 backtest_reuse_days(schema/062), 真判定在执行层 reuse_row
    rd = int(await pool.fetchval(
        "SELECT value FROM config WHERE key='backtest_reuse_days'") or 0)
    reusable = 0
    if rd and row["total"]:
        need = int(oos_v2.window_years(p) * oos_v2.YEAR_DAYS) - 45
        args2 = list(args) + [rd, need]
        reusable = await pool.fetchval(
            "SELECT count(*) FROM strategies s"
            " JOIN backtests b ON b.strategy_id = s.id AND b.symbol = s.symbol"
            f" WHERE {' AND '.join(conds)}"
            f"   AND b.created_at >= now() - make_interval(days => ${len(args) + 1})"
            f"   AND b.to_time - b.from_time >= make_interval(days => ${len(args) + 2})",
            *args2)
    return {"total": row["total"], "tagged": row["tagged"], "symbols": row["symbols"],
            "reusable": reusable, "reuse_days": rd,
            "window_years": oos_v2.window_years(p),
            "batch_limit": p["batch_limit"], "anchor": anchor.isoformat()}


@router.get("/oos_v2/progress")
async def oos_progress(request: Request):
    """进度(两条路合一个出口, 照 v1 定版):
      · 全池清理 = jobs 队列(kind=oos_v2)聚合 — 关页面/重启 api 都不丢, 任何副本能答
      · 点名诊断 = api 内存(几个 ID 秒级, 同步跑完就返回)
    页面只认这一个形状: {running, done, total, current, report_id, phase}"""
    if _progress["running"]:      # 点名诊断正在同步跑
        return {**_progress, "phase": "diagnose"}
    p = await jobs.progress(request.app.state.pool, jobs.OOS_KIND)
    if p["total"]:
        return {"running": p["running"], "done": p["done"], "total": p["total"],
                "current": p["current"] or "", "report_id": None,
                "phase": "backtest" if p["running"] else "judging",
                "errors": p["errors"]}
    # 队列空 = 收尾已完成: 回最新报告 id(归属过滤), 页面从"判定中"跳过去看结果
    uid = identity.scope_uid(request)
    last = await request.app.state.pool.fetchval(
        "SELECT id FROM oos_v2_screens" + (" WHERE owner_id = $1" if uid else "")
        + " ORDER BY id DESC LIMIT 1", *([uid] if uid else []))
    return {**_progress, "phase": "idle", "report_id": _progress["report_id"] or last}


@router.post("/oos_v2/stop")
async def oos_stop(request: Request):
    """停止当前全池批次(2026-08-07 Frank 要: 防误点/变更计划): 删空队列 = 不出报告不打标签。
    正在跑的任务把手头回测跑完(结果回流 backtests 幂等无害), 完成时更新 0 行自然结束。
    点名诊断(同步)不受此按钮管 — 它本来就秒级跑完且零写入。"""
    n = await request.app.state.pool.execute(
        "DELETE FROM jobs WHERE kind=$1", jobs.OOS_KIND)
    deleted = int(n.split()[-1]) if n.split()[-1].isdigit() else 0
    logger.info("oos_v2 stopped: %d jobs deleted", deleted)
    return {"deleted": deleted}


# ---------- 运行(第2步: 点名诊断 = 同步现跑现判, 只读不入库) ----------
class OosRun(BaseModel):
    mode: str = "preview"              # preview=纯报告零动作(默认) / execute=第6步再开
    ids: Optional[list[int]] = None    # 按 ID 点名 = 只读诊断(强制预览, 不入库)
    task: Optional[str] = None         # 任务标签(可选, 记进报告好认)
    limit: Optional[int] = None        # 单次上限(不传 = 用配置 batch_limit)


@router.post("/oos_v2/run")
async def oos_run(req: OosRun, request: Request):
    """两条路(照 v1 定版, 判定逻辑共用 services/oos_v2 一份):
      · 全池清理(不传 ids) = 投 jobs 队列(kind=oos_v2) 秒回 → worker 并行现跑 20 年回测
        → 主节点收尾判定/落报告(oos_v2.finalize)
      · 点名诊断(传 ids) = 本请求内同步现跑现判, 只读不入库(几个 ID, 走队列绕远)
    两条路的回测都是【现跑】(结果 UPSERT 回流 backtests, 结论以报告为准)。"""
    pool = request.app.state.pool
    if req.mode not in ("preview", "execute"):
        raise HTTPException(status_code=400, detail="mode 需为 preview / execute")
    if req.ids and req.mode == "execute":
        raise HTTPException(status_code=400, detail="按 ID 点名 = 只读诊断, 只支持预览模式")

    p = oos_v2.cfg_params(
        await pool.fetchval("SELECT value FROM config WHERE key='oos_v2'") or {})
    anchor = datetime.now(timezone.utc).date()   # 锚点 = 跑批当天(本批冻结)
    conds, args, scope = _scope_conds(req.ids, identity.scope_uid(request))
    if (req.task or "").strip():
        scope["task"] = req.task.strip()
    rows = await pool.fetch(
        "SELECT s.id, s.name, s.symbol, s.status, s.template, s.params,"
        "       s.timeframe, s.metadata"
        f" FROM strategies s WHERE {' AND '.join(conds)} ORDER BY s.symbol, s.id", *args)
    if not rows:
        raise HTTPException(status_code=404, detail=(
            "点名的 ID 不存在" if req.ids else "没有未筛过的空闲策略 — 池子已清完"))

    # ===== 全池清理 = 投 jobs 队列, worker 并行跑回测(第4步) =====
    # 判定不在这里做: 队列跑完由主节点心跳收尾(oos_v2.finalize) — 报告在那一步一次性落库。
    if not req.ids:
        if await jobs.has_active(pool, jobs.OOS_KIND):
            raise HTTPException(status_code=409, detail="已有一批筛选在跑, 等它完成再点")
        limit = req.limit if req.limit else p["batch_limit"]
        targets, not_run = list(rows[:limit]), max(len(rows) - limit, 0)
        if limit:
            scope["limit"] = limit
        # 复用不在这里预筛(2026-08-07 全局统一): 全部照常投队列, 执行层 reuse_row 命中的
        # 任务几毫秒 DONE(不进引擎) — 批量/单ID/v1/oos_v2 同一守卫, 本路由零复用逻辑
        costs = await pool.fetchval(
            "SELECT value FROM config WHERE key='backtest_costs'") or {}
        costs = {k: costs.get(k)
                 for k in ("slippage_points", "commission_points", "spread_points")}
        # 窗口: 锚点往回 window_years 年 → now(引擎 to_time 记实际末根, 不撒谎)
        t_from = oos_v2.anchor_dt(anchor) - timedelta(
            days=oos_v2.window_years(p) * oos_v2.YEAR_DAYS)
        t_to = datetime.now(timezone.utc)
        # 每个任务 payload 带上本次运行配置(收尾从任一 payload 重建 → 不加表不加列;
        # 队列删空 = 没有待收尾的批次, 天然幂等自清理)。每策略一个任务(只跑主品种)。
        run_cfg = {"mode": req.mode, "anchor": anchor.isoformat(), "judge": p,
                   "scope": scope, "owner": getattr(request.state, "user_id", 1),
                   "skipped": [], "not_run": not_run}
        items = [{"strategy_id": s["id"], "name": s["name"], "symbol": s["symbol"],
                  "from": t_from.isoformat(), "to": t_to.isoformat(), "costs": costs,
                  "run": run_cfg} for s in targets]
        await jobs.submit_batch(pool, items, jobs.OOS_KIND)
        logger.info("oos_v2 submitted: %d jobs (mode=%s, anchor=%s)",
                    len(items), req.mode, anchor)
        return {"queued": True, "jobs": len(items), "strategies": len(targets),
                "mode": req.mode, "anchor": anchor.isoformat(), "not_run": not_run}

    # ===== 点名诊断: api 内同步现跑现判(只读, 不入库) =====

    # 引擎配置与批量回测同一来源(对比三铁律: 成本/oos/trail 回落一致)
    costs = await pool.fetchval("SELECT value FROM config WHERE key='backtest_costs'") or {}
    costs = {k: costs.get(k)
             for k in ("slippage_points", "commission_points", "spread_points")}
    oos_split = await pool.fetchval(
        "SELECT value FROM config WHERE key='backtest_oos_split'") or 0.7
    trail_default = await pool.fetchval("SELECT value FROM config WHERE key='trail_default'")
    syms_meta = {r["symbol"]: dict(r) for r in await pool.fetch(
        "SELECT symbol, point, broker FROM symbols")}
    t_to = datetime.now(timezone.utc)
    t_from = oos_v2.anchor_dt(anchor) - timedelta(
        days=oos_v2.window_years(p) * oos_v2.YEAR_DAYS)

    m1_cache: dict = {"sym": None, "m1": None}

    async def _fresh_bt(strat):
        """现跑一发全窗回测(与 jobs._run_one 同一配方) → {trades, ...};
        品种不在 symbols 表 / 无 M1 → 抛错(judge_one 记 skip, 铁则1 永不淘汰)"""
        sym = strat["symbol"]
        if sym not in syms_meta:
            raise ValueError(f"{sym} 不在 symbols 表")
        # 复用守卫(2026-08-07 全局统一, 唯一实现在 backtest.reuse_row): 命中即不重跑
        row = await backtest.reuse_row(pool, strat["id"], sym, t_from, t_to)
        if row:
            return {"trades": row["trades"]}
        if m1_cache["sym"] != sym:
            m1_cache["m1"] = await backtest.load_m1(pool, sym, t_from, t_to)
            m1_cache["sym"] = sym
        m1 = m1_cache["m1"]
        if m1 is None:
            raise ValueError(f"{sym} 无 M1 数据")
        params = strat["params"]
        if isinstance(params, dict) and not params.get("trail") \
                and isinstance(trail_default, dict) and trail_default.get("active"):
            params = {**params, "trail": trail_default}
        gate = await regime.gate_for(pool, strat["metadata"], sym)
        result = await asyncio.to_thread(
            backtest.run_backtest, m1, strat["template"], params,
            syms_meta[sym]["point"], strat["timeframe"], oos_split=oos_split,
            gate=gate, **costs)
        cov_from = datetime.fromtimestamp(int(m1["time"][0]), tz=timezone.utc)
        cov_to = datetime.fromtimestamp(int(m1["time"][-1]), tz=timezone.utc)
        # 结果 UPSERT 回流(from/to = 实际首末根, 标签不撒谎; 覆盖 20 年窗为既定行为, 结论以报告为准)
        await pool.execute(
            "INSERT INTO backtests"
            " (strategy_id, from_time, to_time, symbol, broker, metrics, trades)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)"
            " ON CONFLICT (strategy_id, symbol) DO UPDATE SET"
            "   from_time=EXCLUDED.from_time, to_time=EXCLUDED.to_time,"
            "   broker=EXCLUDED.broker, metrics=EXCLUDED.metrics,"
            "   trades=EXCLUDED.trades, created_at=now()",
            strat["id"], cov_from, cov_to, sym, syms_meta[sym]["broker"],
            result["metrics"], result["trades"])
        return {"trades": result["trades"]}

    # running 标志紧贴 try 设置(中间零 await): 任何异常路径都必经 finally 复位
    if _progress["running"]:
        raise HTTPException(status_code=409, detail="已有一批诊断在跑, 等它完成再点")
    _progress.update(running=True, done=0, current="", report_id=None, total=len(rows))
    details = []
    try:
        # 按品种排序(SQL 已排): 同品种连续命中 M1 单槽缓存
        for strat in rows:
            _progress["current"] = f"#{strat['id']} {strat['name']} @ {strat['symbol']}"
            try:
                bt_row = await _fresh_bt(strat)
            except Exception as e:
                logger.warning("oos_v2 fresh backtest #%s failed: %s", strat["id"], e)
                bt_row = {"error": str(e)}
            details.append(oos_v2.judge_one(dict(strat), bt_row, anchor, p))
            _progress["done"] += 1
    finally:
        _progress.update(running=False, current="")

    summary = oos_v2.summarize(details, req.mode, archived=0, not_run=0)
    # 点名诊断也落库报告(2026-08-08 Frank 改: 单ID也要有历史可溯源);
    # 动作恒为零 — 不追加 tags 不归档(出池履历只属于全池清理的收尾)
    rid = await pool.fetchval(
        "INSERT INTO oos_v2_screens"
        " (mode, anchor, scope, params, summary, details, owner_id)"
        " VALUES ('preview', $1, $2, $3, $4, $5, $6) RETURNING id",
        anchor, scope, p, summary, details, getattr(request.state, "user_id", 1))
    return {"report_id": rid, "mode": req.mode, "anchor": anchor.isoformat(),
            "scope": scope, "params": p, "summary": summary, "details": details}


# ---------- 报告回看(归属过滤照 v0.5: owner 只见自己的, admin 全见) ----------
@router.get("/oos_v2/reports")
async def oos_reports(request: Request, limit: int = 30):
    uid = identity.scope_uid(request)
    rows = await request.app.state.pool.fetch(
        "SELECT id, created_at, mode, anchor, scope, params, summary"
        " FROM oos_v2_screens" + (" WHERE owner_id = $2" if uid else "")
        + " ORDER BY id DESC LIMIT $1",
        min(max(limit, 1), 200), *([uid] if uid else []))
    return {"reports": [{**dict(r), "created_at": r["created_at"].isoformat(),
                         "anchor": r["anchor"].isoformat()} for r in rows]}


@router.get("/oos_v2/reports/{report_id}")
async def oos_report(report_id: int, request: Request, offset: int = 0,
                     limit: int = 50, verdict: Optional[str] = None,
                     sort: str = "", dir: str = "desc"):
    """报告明细(服务端分页 + 排序, 照 v0.5 定版: 几千行全量进浏览器会卡死, 排序须全量排)。
    periods 本身就轻(每策略 ~1KB), 不需要 v0.5 那种深层字段剥离 — 行内即全部数字。"""
    if verdict not in (None, "", "pass", "fail", "skip"):
        raise HTTPException(status_code=400, detail="verdict 需为 pass/fail/skip")
    order_expr = _sort_expr(sort) if sort else None
    if sort and order_expr is None:
        raise HTTPException(status_code=400,
                            detail=f"sort 需为 {sorted(SORTS)} 或 seg<i>:train|test")
    limit = min(max(limit, 1), 200)
    cond = " WHERE e->>'verdict' = $2" if verdict else ""
    args = [report_id] + ([verdict] if verdict else []) + [max(offset, 0), limit]
    p_off, p_lim = f"${len(args) - 1}", f"${len(args)}"
    if order_expr:
        order = f"{order_expr} {'ASC' if dir == 'asc' else 'DESC'} NULLS LAST, ord"
    else:
        order = "rk, ord"   # 默认通过优先(pass→fail→skip, 组内原序)
    sql = (
        "SELECT r.id, r.created_at, r.mode, r.anchor, r.scope, r.params, r.summary,"
        "       r.owner_id,"
        # pass/fail ID 全量名单(2026-08-08 Frank 定: 报告头可复制, 也是筛选器串联的接口)
        "      (SELECT COALESCE(jsonb_agg((e->>'id')::int ORDER BY (e->>'id')::int), '[]'::jsonb)"
        "         FROM jsonb_array_elements(r.details) e"
        "        WHERE e->>'verdict' = 'pass') AS pass_ids,"
        "      (SELECT COALESCE(jsonb_agg((e->>'id')::int ORDER BY (e->>'id')::int), '[]'::jsonb)"
        "         FROM jsonb_array_elements(r.details) e"
        "        WHERE e->>'verdict' = 'fail') AS fail_ids,"
        f"      (SELECT count(*) FROM jsonb_array_elements(r.details) e{cond}) AS total,"
        f"      (SELECT jsonb_agg(s.e ORDER BY {order.replace('ord', 's.ord').replace('rk', 's.rk')})"
        "         FROM ("
        "          SELECT e, ord, CASE e->>'verdict' WHEN 'pass' THEN 0"
        "                              WHEN 'fail' THEN 1 ELSE 2 END AS rk"
        "            FROM jsonb_array_elements(r.details)"
        f"                            WITH ORDINALITY AS t(e, ord){cond}"
        f"         ORDER BY {order} OFFSET {p_off} LIMIT {p_lim}) s) AS details"
        " FROM oos_v2_screens r WHERE r.id = $1")
    row = await request.app.state.pool.fetchrow(sql, *args)
    uid = identity.scope_uid(request)
    if row is None or (uid and row["owner_id"] != uid):   # 别人的报告 = 404 不暴露存在性
        raise HTTPException(status_code=404, detail="report not found")
    return {**{k: row[k] for k in row.keys() if k != "owner_id"},
            "details": row["details"] or [],
            "created_at": row["created_at"].isoformat(), "anchor": row["anchor"].isoformat(),
            "offset": max(offset, 0), "limit": limit, "verdict": verdict or "",
            "sort": sort, "dir": dir}
