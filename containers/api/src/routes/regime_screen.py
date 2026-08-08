"""v0.5 Regime 筛选(现跑回测 + 切片判定 + 落库报告) — 测试性质可插拔功能。

漏斗关 1.5: 每个策略【现跑】总计年窗口的回测(与批量回测同一配方: load_m1 + 悲观撮合 +
成本/oos/trail 同一 config 来源, 结果 UPSERT 回流 backtests — 顺手治愈历史上"请求 20 年
实际只有几个月数据"的假窗口行), 然后逐笔贴 regime 时间线切片判稳健: 每刀 = 近 b 年 vs 剩余,
存在 ≥N 个格子全切分前后段都(笔数≥地板 且 净点>阈值 且 PF>阈值)才通过。

两条路(2026-08-05 队列化定版, 六步走完并逐字段比对零差异):
  · 全池清理(不传 ids) = 投 jobs 队列(kind=regime_screen)秒回 → worker 并行现跑 →
    主节点收尾判定/落报告/执行动作(services/screen.finalize)。500 个 ~20分钟 → ~3分钟
  · 点名诊断(传 ids)   = 本请求内同步现跑现判, 报告落库但零动作(任意状态可点名, 强制预览;
    2026-08-08 起单ID报告也进历史 — 不打标签不归档)
判定逻辑不在本文件 — 全系统一份在 services/screen.py, 两条路共用(结果必然一致)。
执行动作只有两个系统本来就支持的写入: 通过 → basis 追加 ｜regime筛过#报告id;
未过 → 归档(死因 regime_unstable, 可逆) — 且只在 finalize 里一次性发生。
自包含: 判据参数读写(config regime_screen)在本文件; 移除手册见 docs/2.regime_dirction/v0.5。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import backtest, identity, jobs, regime, screen

logger = logging.getLogger(__name__)
router = APIRouter()

TAG = screen.TAG   # basis 标签词根(唯一定义在 services/screen.py)

# 报告明细可排序列(白名单, 值=JSONB 表达式): 排序在库里做, 覆盖全量而非当页。
# PF 的 ∞(无亏损, 存 null)按最大值参与排序; 跳过的行没有这些字段 → NULL → 恒沉底
SORTS = {
    "id": "(e->>'id')::bigint",
    "symbol": "e->>'symbol'",
    "name": "e->>'name'",
    "trades": "(e->>'trades')::numeric",
    "net": "(e->'total'->>'net')::numeric",
    "pf": ("CASE WHEN e ? 'total' AND e->'total'->>'pf' IS NULL THEN 1e18"
           "     ELSE (e->'total'->>'pf')::numeric END"),
    "cells": "jsonb_array_length(e->'pass_cells')",
    "verdict": ("CASE e->>'verdict' WHEN 'pass' THEN 0 WHEN 'fail' THEN 1 ELSE 2 END"),
}

# 运行进度(点名诊断走 api 内存; 全池清理走 jobs 队列, 见 screen_progress)
_progress = {"running": False, "done": 0, "total": 0, "current": "", "report_id": None}


@router.get("/regime_screen/progress")
async def screen_progress(request: Request):
    """进度(2026-08-05 起两条路合一个出口):
      · 全池清理 = jobs 队列(kind=regime_screen)聚合 — 关页面/重启 api 都不丢, 任何副本能答
      · 点名诊断 = api 内存(几个 ID 秒级, 同步跑完就返回)
    页面只认这一个形状: {running, done, total, current, report_id, phase}"""
    if _progress["running"]:      # 点名诊断正在同步跑
        return {**_progress, "phase": "diagnose"}
    p = await jobs.progress(request.app.state.pool, jobs.SCREEN_KIND)
    if p["total"]:
        return {"running": p["running"], "done": p["done"], "total": p["total"],
                "current": p["current"] or "", "report_id": None,
                "phase": "backtest" if p["running"] else "judging",
                "errors": p["errors"]}
    # 队列空 = 收尾已完成: 回最新报告 id(归属过滤), 页面从"判定中"跳过去看结果
    uid = identity.scope_uid(request)
    last = await request.app.state.pool.fetchval(
        "SELECT id FROM regime_screens" + (" WHERE owner_id = $1" if uid else "")
        + " ORDER BY id DESC LIMIT 1", *([uid] if uid else []))
    return {**_progress, "phase": "idle", "report_id": _progress["report_id"] or last}


# ---------- 判据参数(config regime_screen 唯一源; 切分 = 近 b 年 vs 剩余, b 可小数) ----------
class ScreenParams(BaseModel):
    window_years: float = 5            # 总计回测窗口(年): M1 覆盖不足此数 = 跳过不判
    boundaries_years: list[float]      # 切分点(年, 可小数): 每刀 = 近 b 年(后段) vs 剩余(前段)
    min_cell_trades: int = 5           # 格内地板笔数(前后两段各自)
    min_pass_cells: int = 1            # 至少几个格子全切分达标才算通过
    min_net_points: float = 0          # 净点阈值(严格大于; 默认 0 ≡ 净点为正)
    min_pf: float = 1.0                # PF 阈值(严格大于; 默认 1 与净点>0 等价, 调高即收紧)


@router.post("/regime_screen/params")
async def screen_params_save(req: ScreenParams, request: Request):
    if not 1 <= req.window_years <= 30:
        raise HTTPException(status_code=400, detail="总计年需在 1~30")
    bs = sorted({round(float(b), 2) for b in req.boundaries_years})
    if not bs or any(b <= 0 or b >= req.window_years for b in bs):
        raise HTTPException(status_code=400,
                            detail="切分点需在 0 与总计年之间(近 b 年 vs 剩余, 可小数)")
    if not 1 <= req.min_cell_trades <= 100:
        raise HTTPException(status_code=400, detail="格内最少笔数需在 1~100")
    if not 1 <= req.min_pass_cells <= 8:
        raise HTTPException(status_code=400, detail="至少合格格数需在 1~8")
    if req.min_net_points < 0 or req.min_pf < 0:
        raise HTTPException(status_code=400, detail="净点/PF 阈值不能为负")
    val = {"window_years": req.window_years, "boundaries_years": bs,
           "min_cell_trades": req.min_cell_trades, "min_pass_cells": req.min_pass_cells,
           "min_net_points": req.min_net_points, "min_pf": req.min_pf}
    await request.app.state.pool.execute(
        "INSERT INTO config (key, value) VALUES ('regime_screen', $1)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", val)
    return val


# ---------- 范围(plan 与 run 共用同一判据, 预估数 = 实跑数) ----------
def _scope_conds(req_ids, req_symbols, uid):
    """默认 = 全部未筛过的空闲策略(轮番清理, SQL 直接排掉已筛过); ID 点名 = 任意状态只读诊断。
    symbols(main/all)不是范围过滤是判定数据源, 只记进 scope 供报告展示。"""
    if req_ids:
        conds, args = [], [req_ids]
        conds.append(f"s.id = ANY(${len(args)})")
        scope = {"ids": req_ids, "symbols": req_symbols}
    else:
        conds, args = ["s.status = 'CANDIDATE'",
                       f"COALESCE(s.basis, '') NOT LIKE '%{TAG}%'"], []
        scope = {"pool": "unscreened", "symbols": req_symbols}
    if uid:
        args.append(uid)
        conds.append(f"s.owner_id = ${len(args)}")
    return conds, args, scope


async def _m1_span(pool, sym: str):
    """品种 M1 实际覆盖(首根/末根), 无数据 = None — plan 预估与 run 同一口径"""
    lo = await pool.fetchval(
        "SELECT time FROM historical_bars WHERE symbol=$1 AND timeframe='M1'"
        " ORDER BY time LIMIT 1", sym)
    hi = await pool.fetchval(
        "SELECT time FROM historical_bars WHERE symbol=$1 AND timeframe='M1'"
        " ORDER BY time DESC LIMIT 1", sym)
    return (lo, hi) if lo and hi else None


@router.get("/regime_screen/plan")
async def screen_plan(request: Request, ids: Optional[str] = None, symbols: str = "main"):
    """运行预估(页面预览行实时刷): 匹配多少 / 可现跑多少 / 哪些品种 M1 不足 — 纯读零动作"""
    pool = request.app.state.pool
    id_list = None
    if ids:
        try:
            id_list = [int(x) for x in ids.replace("，", ",").split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="ID 列表需为逗号分隔的整数")
    p = screen.cfg_params(
        await pool.fetchval("SELECT value FROM config WHERE key='regime_screen'") or {})
    need_days = int(p["window_years"] * 365.25) - 45
    conds, args, _ = _scope_conds(id_list, symbols, identity.scope_uid(request))
    row = await pool.fetchrow(
        "SELECT count(*)::int AS total,"
        f"      count(*) FILTER (WHERE COALESCE(s.basis, '') LIKE '%{TAG}%')::int AS tagged"
        f" FROM strategies s WHERE {' AND '.join(conds)}", *args)
    # 品种 M1 覆盖检查(现跑口径): 主货币=范围内策略的品种; 全货币=另加全部下载品种
    per_sym = {r["symbol"]: r["n"] for r in await pool.fetch(
        f"SELECT s.symbol, count(*) FILTER (WHERE COALESCE(s.basis, '') NOT LIKE '%{TAG}%')"
        f"::int AS n FROM strategies s WHERE {' AND '.join(conds)} GROUP BY s.symbol", *args)}
    check_syms = set(per_sym)
    if symbols == "all":
        check_syms |= {r["symbol"] for r in await pool.fetch(
            "SELECT symbol FROM symbols WHERE download")}
    ok_syms, insufficient = [], []
    for sym in sorted(check_syms):
        span = await _m1_span(pool, sym)
        (ok_syms if span and (span[1] - span[0]).days >= need_days
         else insufficient).append(sym)
    runnable = sum(n for sym, n in per_sym.items() if sym in ok_syms)
    return {"total": row["total"], "tagged": row["tagged"], "runnable": runnable,
            "need_days": need_days, "ok_symbols": ok_syms, "insufficient": insufficient,
            "runs_per_strategy": len(ok_syms) if symbols == "all" else 1}


# 切片判定/收尾在 services/screen.py(判定逻辑全系统一份, 与队列收尾共用) —
# 本文件只保留"点名诊断"的同步现跑路径, 判定调 screen.judge_symbol / screen.judge_one


# ---------- 运行(现跑回测 + 切片判定; 品种外层循环 = M1 单槽缓存零抖动) ----------
class ScreenRun(BaseModel):
    mode: str = "preview"              # preview=纯报告零动作 / execute=打标签+归档
    ids: Optional[list[int]] = None    # 按 ID 点名 = 只读诊断(强制预览, 报告不入库)
    task: Optional[str] = None         # 任务标签(可选): 给这次清理起个名, 记进报告好认
    version: Optional[int] = None      # regime 版本, 不传 = 当前默认
    symbols: str = "main"              # 判定数据源: main=只跑主品种 / all=全部下载品种都得过
    limit: Optional[int] = None        # 单次最多判多少个策略(超出的下次再跑), 不传 = 不限


@router.post("/regime_screen/run")
async def screen_run(req: ScreenRun, request: Request):
    """两条路(2026-08-05 队列化定版, 判定逻辑共用 services/screen 一份):
      · 全池清理(不传 ids) = 投 jobs 队列(kind=regime_screen) 秒回 → worker 并行现跑回测
        → 主节点收尾判定/落报告/打标签/归档(screen.finalize)
      · 点名诊断(传 ids) = 本请求内同步现跑现判, 只读不入库(几个 ID 秒级, 走队列绕远)
    两条路的回测都是【现跑】(结果 UPSERT 回流 backtests, 假窗口旧行被真数据覆盖)。"""
    pool = request.app.state.pool
    if req.mode not in ("preview", "execute"):
        raise HTTPException(status_code=400, detail="mode 需为 preview / execute")
    if req.symbols not in ("main", "all"):
        raise HTTPException(status_code=400, detail="symbols 需为 main / all")
    if req.ids and req.mode == "execute":
        raise HTTPException(status_code=400, detail="按 ID 点名 = 只读诊断, 只支持预览模式")

    p = screen.cfg_params(
        await pool.fetchval("SELECT value FROM config WHERE key='regime_screen'") or {})
    need_days = int(p["window_years"] * 365.25) - 45
    p["boundaries_years"] = [b for b in p["boundaries_years"] if b < p["window_years"]]
    if not p["boundaries_years"]:
        raise HTTPException(status_code=400, detail="判据无效: 没有小于总计年的切分点")

    if req.version is not None:
        if not await pool.fetchval("SELECT 1 FROM regime_versions WHERE id=$1", req.version):
            raise HTTPException(status_code=400, detail=f"regime 版本 v{req.version} 不存在")
        vid = int(req.version)
    else:
        vid, _ = await regime.active_version(pool)

    conds, args, scope = _scope_conds(req.ids, req.symbols, identity.scope_uid(request))
    if req.limit:
        scope["limit"] = req.limit
    if (req.task or "").strip():
        scope["task"] = req.task.strip()
    rows = await pool.fetch(
        "SELECT s.id, s.name, s.symbol, s.basis, s.status, s.template, s.params,"
        "       s.timeframe, s.metadata"
        f" FROM strategies s WHERE {' AND '.join(conds)} ORDER BY s.symbol, s.id", *args)
    if not rows:
        raise HTTPException(status_code=404, detail=(
            "点名的 ID 不存在" if req.ids else "没有未筛过的空闲策略 — 池子已清完"))

    # 先分拣: 已筛过 = skip 不占额度; 其余按单次上限截取(按品种排序 = 现跑缓存友好)
    details, targets, not_run = [], [], 0
    for r in rows:
        if r["basis"] and TAG in r["basis"]:
            details.append({"id": r["id"], "name": r["name"], "symbol": r["symbol"],
                            "status": r["status"], "verdict": "skip",
                            "reason": "已筛过(幂等不重复)"})
        elif req.limit and len(targets) >= req.limit:
            not_run += 1
        else:
            targets.append(r)

    if req.symbols == "all":
        run_syms = [r["symbol"] for r in await pool.fetch(
            "SELECT symbol FROM symbols WHERE download ORDER BY symbol")]

    # ===== 全池清理 = 投 jobs 队列, worker 并行跑回测(2026-08-05 步骤2) =====
    # 判定不在这里做: 队列跑完由主节点收尾(步骤3) — 报告/打标签/归档都在那一步一次性发生。
    # 点名诊断(req.ids)保持下面的同步路径: 几个 ID 秒级, 走队列绕远。
    if not req.ids:
        if await jobs.has_active(pool, jobs.SCREEN_KIND):
            raise HTTPException(status_code=409, detail="已有一批筛选在跑, 等它完成再点")
        t_to = datetime.now(timezone.utc)
        t_from = t_to - timedelta(days=p["window_years"] * 365.25)
        costs = await pool.fetchval(
            "SELECT value FROM config WHERE key='backtest_costs'") or {}
        costs = {k: costs.get(k)
                 for k in ("slippage_points", "commission_points", "spread_points")}
        # 每个任务 payload 带上本次运行配置(判定阶段从任一 payload 重建 → 不需要新表新列;
        # 队列删空 = 没有待收尾的批次, 天然幂等自清理)
        run_cfg = {"mode": req.mode, "version": vid, "symbols": req.symbols,
                   "judge": p, "scope": scope, "owner": getattr(request.state, "user_id", 1),
                   "skipped": details, "not_run": not_run}
        # 全货币 = 全部下载品种 ∪ 策略自己的主品种(主品种不在下载列表时也必须判它 —
        # 否则主品种没有回测行 → 整策略被跳过, 与同步路径不一致)
        items = [{"strategy_id": s["id"], "name": s["name"], "symbol": sym,
                  "from": t_from.isoformat(), "to": t_to.isoformat(), "costs": costs,
                  "main_symbol": s["symbol"], "run": run_cfg}
                 for s in targets
                 for sym in (sorted(set(run_syms) | {s["symbol"]})
                             if req.symbols == "all" else [s["symbol"]])]
        if not items:
            raise HTTPException(status_code=404, detail="没有可判定的策略(都已筛过?)")
        await jobs.submit_batch(pool, items, jobs.SCREEN_KIND)
        logger.info("regime_screen submitted: %d jobs (%d strategies, mode=%s)",
                    len(items), len(targets), req.mode)
        return {"queued": True, "jobs": len(items), "strategies": len(targets),
                "mode": req.mode, "version": vid, "skipped": len(details),
                "not_run": not_run}

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
    t_from = t_to - timedelta(days=p["window_years"] * 365.25)

    m1_cache: dict = {"sym": None, "m1": None}

    async def _fresh_bt(strat, sym):
        """现跑一发总计年回测(与 jobs._run_one 同一配方) → bt 行 dict;
        None = 该品种 M1 覆盖不足总计年。结果 UPSERT 回流(from/to = 实际首末根, 标签不撒谎)"""
        # 复用守卫(2026-08-07 全局统一, 唯一实现在 backtest.reuse_row): 命中即不重跑
        row = await backtest.reuse_row(pool, strat["id"], sym, t_from, t_to)
        if row:
            return {"symbol": sym, "from_time": row["from_time"],
                    "to_time": row["to_time"], "trades": row["trades"]}
        if m1_cache["sym"] != sym:
            m1_cache["m1"] = await backtest.load_m1(pool, sym, t_from, t_to)
            m1_cache["sym"] = sym
        m1 = m1_cache["m1"]
        if m1 is None:
            return None
        cov_from = datetime.fromtimestamp(int(m1["time"][0]), tz=timezone.utc)
        cov_to = datetime.fromtimestamp(int(m1["time"][-1]), tz=timezone.utc)
        if (cov_to - cov_from).days < need_days:
            return None
        params = strat["params"]
        if isinstance(params, dict) and not params.get("trail") \
                and isinstance(trail_default, dict) and trail_default.get("active"):
            params = {**params, "trail": trail_default}
        gate = await regime.gate_for(pool, strat["metadata"], sym)
        result = await asyncio.to_thread(
            backtest.run_backtest, m1, strat["template"], params,
            syms_meta[sym]["point"], strat["timeframe"], oos_split=oos_split,
            gate=gate, **costs)
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
        return {"symbol": sym, "from_time": cov_from, "to_time": cov_to,
                "trades": result["trades"]}

    tls: dict = {}                      # 品种时间线缓存(同一 vid)
    judged: dict = {}                   # 策略 id → {品种: 判定结果}
    # 品种外层循环: 同品种的现跑连续命中 M1 单槽缓存(与 jobs 抢单按品种排序同思路)
    plan_syms = sorted({t["symbol"] for t in targets}) if req.symbols == "main" \
        else sorted(set(run_syms) | {t["symbol"] for t in targets})
    # running 标志紧贴 try 设置(中间零 await): 任何异常路径都必经 finally 复位, 不会卡死 409
    if _progress["running"]:
        raise HTTPException(status_code=409, detail="已有一批筛选在跑, 等它完成再点")
    _progress.update(running=True, done=0, current="", report_id=None,
                     total=len(targets) * (len(run_syms) if req.symbols == "all" else 1))
    try:
        for sym in plan_syms:
            if sym not in syms_meta:
                continue
            for strat in targets:
                if req.symbols == "main" and strat["symbol"] != sym:
                    continue
                _progress["current"] = f"#{strat['id']} {strat['name']} @ {sym}"
                try:
                    bt = await _fresh_bt(strat, sym)
                except Exception as e:
                    logger.warning("screen fresh backtest #%s %s failed: %s",
                                   strat["id"], sym, e)
                    judged.setdefault(strat["id"], {})[sym] = {"error": str(e)}
                    _progress["done"] += 1
                    continue
                if bt is None:      # M1 不足: 主品种=整策略跳过; 跨品种=不纳入要求
                    judged.setdefault(strat["id"], {})[sym] = None
                else:
                    judged.setdefault(strat["id"], {})[sym] = \
                        await screen.judge_symbol(pool, tls, bt, vid, p)
                _progress["done"] += 1
    finally:
        _progress.update(running=False, current="")

    # 结论构建 = 共用 screen.judge_one(与队列收尾同一份判定, 两条路结果必然一致);
    # 点名诊断只读, 动作(tag/archive)一律忽略
    for r in targets:
        d, _action = screen.judge_one(dict(r), judged.get(r["id"], {}), p, req.symbols)
        details.append(d)

    summary = screen.summarize(details, [], req.mode, not_run)
    # 点名诊断也落库报告(2026-08-08 Frank 改: 单ID也要有历史可溯源);
    # 动作恒为零 — 不打标签不归档(执行只属于全池清理的收尾, 见 services/screen)
    rid = await pool.fetchval(
        "INSERT INTO regime_screens"
        " (mode, version_id, scope, params, summary, details, owner_id)"
        " VALUES ('preview', $1, $2, $3, $4, $5, $6) RETURNING id",
        vid, scope, p, summary, details, getattr(request.state, "user_id", 1))
    return {"report_id": rid, "mode": req.mode, "version": vid,
            "scope": scope, "params": p, "summary": summary, "details": details}


# ---------- 报告回看(归属过滤, 2026-08-04 Frank 定: owner 只见自己的, admin 全见) ----------
@router.get("/regime_screen/reports")
async def screen_reports(request: Request, limit: int = 30):
    uid = identity.scope_uid(request)
    rows = await request.app.state.pool.fetch(
        "SELECT id, created_at, mode, version_id, scope, params, summary"
        " FROM regime_screens" + (" WHERE owner_id = $2" if uid else "")
        + " ORDER BY id DESC LIMIT $1",
        min(max(limit, 1), 200), *([uid] if uid else []))
    return {"reports": [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]}


@router.get("/regime_screen/reports/{report_id}")
async def screen_report(report_id: int, request: Request, offset: int = 0,
                        limit: int = 50, verdict: Optional[str] = None,
                        sort: str = "", dir: str = "desc"):
    """报告明细(服务端分页 + 排序 + 结论级瘦身, 2026-08-06 定版):
      · 6000 行全量进浏览器会卡死 → 库里切片, 一页只回 limit 行
      · 排序也在库里做(2026-08-06 Frank 要): 前端只能排当页 50 行, 排出来的"最好"是假的;
        sort 白名单见 SORTS, 不传 = 通过优先(pass→fail→skip, 组内原序)
      · splits_stat 占 details 81% 一律剥掉, 展开时走 /reports/{id}/strategy/{sid} 单取
      · verdict=pass/fail/skip 服务端过滤(总数按过滤后算)
    库里 details 仍存全量(审计"为什么被归档"不缩水)"""
    if verdict not in (None, "", "pass", "fail", "skip"):
        raise HTTPException(status_code=400, detail="verdict 需为 pass/fail/skip")
    if sort and sort not in SORTS:
        raise HTTPException(status_code=400, detail=f"sort 需为 {sorted(SORTS)}")
    limit = min(max(limit, 1), 200)
    cond = " WHERE e->>'verdict' = $2" if verdict else ""
    args = [report_id] + ([verdict] if verdict else []) + [max(offset, 0), limit]
    p_off, p_lim = f"${len(args) - 1}", f"${len(args)}"
    # 排序键(默认通过优先) + 方向; 无值行(跳过的没净点/PF)恒沉底 = NULLS LAST
    if sort:
        order = f"{SORTS[sort]} {'ASC' if dir == 'asc' else 'DESC'} NULLS LAST, ord"
    else:
        order = "rk, ord"
    sql = (
        "SELECT r.id, r.created_at, r.mode, r.version_id, r.scope, r.params, r.summary,"
        "       r.owner_id,"
        # pass/fail ID 全量名单(2026-08-08 Frank 定: 报告头可复制, 也是筛选器串联的接口)
        "      (SELECT COALESCE(jsonb_agg((e->>'id')::int ORDER BY (e->>'id')::int), '[]'::jsonb)"
        "         FROM jsonb_array_elements(r.details) e"
        "        WHERE e->>'verdict' = 'pass') AS pass_ids,"
        "      (SELECT COALESCE(jsonb_agg((e->>'id')::int ORDER BY (e->>'id')::int), '[]'::jsonb)"
        "         FROM jsonb_array_elements(r.details) e"
        "        WHERE e->>'verdict' = 'fail') AS fail_ids,"
        f"      (SELECT count(*) FROM jsonb_array_elements(r.details) e{cond}) AS total,"
        f"      (SELECT jsonb_agg(s.e - 'splits_stat' ORDER BY {order.replace('ord', 's.ord')}"
        f"                        ) FROM ("
        "          SELECT e, ord, CASE e->>'verdict' WHEN 'pass' THEN 0"
        "                              WHEN 'fail' THEN 1 ELSE 2 END AS rk"
        "            FROM jsonb_array_elements(r.details)"
        f"                            WITH ORDINALITY AS t(e, ord){cond}"
        f"         ORDER BY {order} OFFSET {p_off} LIMIT {p_lim}) s) AS details"
        " FROM regime_screens r WHERE r.id = $1")
    row = await request.app.state.pool.fetchrow(sql, *args)
    uid = identity.scope_uid(request)
    if row is None or (uid and row["owner_id"] != uid):   # 别人的报告 = 404 不暴露存在性
        raise HTTPException(status_code=404, detail="report not found")
    return {**{k: row[k] for k in row.keys() if k != "owner_id"},
            "details": row["details"] or [],
            "created_at": row["created_at"].isoformat(),
            "offset": max(offset, 0), "limit": limit, "verdict": verdict or "",
            "sort": sort, "dir": dir}


@router.get("/regime_screen/reports/{report_id}/strategy/{sid}")
async def screen_report_strategy(report_id: int, sid: int, request: Request):
    """单策略深层数字(展开明细按需取): 格×切分前后段 净点/PF/笔数"""
    row = await request.app.state.pool.fetchrow(
        "SELECT r.owner_id, e AS item FROM regime_screens r,"
        "       jsonb_array_elements(r.details) e"
        " WHERE r.id = $1 AND (e->>'id')::int = $2", report_id, sid)
    uid = identity.scope_uid(request)
    if row is None or (uid and row["owner_id"] != uid):
        raise HTTPException(status_code=404, detail="not found")
    it = row["item"]
    return {"id": sid, "cells_stat": it.get("cells_stat") or {},
            "splits_stat": it.get("splits_stat") or {}}
