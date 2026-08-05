"""v0.5 Regime 筛选(现跑回测 + 切片判定 + 落库报告) — 测试性质可插拔功能。

漏斗关 1.5(2026-08-04 与 Frank 定"不用现成的"): 每个策略【现跑】总计年窗口的回测
(与批量回测同一配方: load_m1 + 悲观撮合 + 成本/oos/trail 同一 config 来源, 结果 UPSERT
回流 backtests — 顺手治愈历史上"请求 20 年实际只有几个月数据"的假窗口行), 然后逐笔
贴 regime 时间线切片判稳健: 每刀 = 近 b 年 vs 剩余, 存在 ≥N 个格子全切分前后段都
(笔数≥地板 且 净点>阈值 且 PF>阈值)才通过。
执行动作只有两个系统本来就支持的写入: 通过 → basis 追加 ｜regime筛过#报告id;
未过 → 归档(死因 regime_unstable, 可逆)。范围默认 = 全部未筛过的空闲策略(轮番清理);
按 ID 点名 = 只读诊断(任意状态, 强制预览, 报告不入库)。
自包含: 判据参数读写(config regime_screen)也在本文件; 移除手册见 docs/2.regime_dirction/v0.5。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import backtest, identity, regime

logger = logging.getLogger(__name__)
router = APIRouter()

TAG = "regime筛过"   # basis 标签词根: 已含即幂等跳过; 列表页搜"标签/生因"可一键捞幸存者

# 运行进度(api 内存, 页面 AJAX 轮询 — 与下载编排进度同一裁定: 筛选无状态,
# api 挂了重新触发即可, 进度 jobs 化属过度设计不做)
_progress = {"running": False, "done": 0, "total": 0, "current": "", "report_id": None}


@router.get("/regime_screen/progress")
async def screen_progress():
    return _progress


# ---------- 判据参数(config regime_screen 唯一源; 切分 = 近 b 年 vs 剩余, b 可小数) ----------
class ScreenParams(BaseModel):
    window_years: float = 5            # 总计回测窗口(年): M1 覆盖不足此数 = 跳过不判
    boundaries_years: list[float]      # 切分点(年, 可小数): 每刀 = 近 b 年(后段) vs 剩余(前段)
    min_cell_trades: int = 5           # 格内地板笔数(前后两段各自)
    min_pass_cells: int = 1            # 至少几个格子全切分达标才算通过
    min_net_points: float = 0          # 净点阈值(严格大于; 默认 0 ≡ 净点为正)
    min_pf: float = 1.0                # PF 阈值(严格大于; 默认 1 与净点>0 等价, 调高即收紧)


def _cfg_params(cfg: dict) -> dict:
    """config → 判据(带默认): run/plan/params 三处同一口径"""
    return {"window_years": float(cfg.get("window_years") or 5),
            "boundaries_years": sorted(cfg.get("boundaries_years") or [1, 2, 3, 4]),
            "min_cell_trades": int(cfg.get("min_cell_trades") or 5),
            "min_pass_cells": int(cfg.get("min_pass_cells") or 1),
            "min_net_points": float(cfg.get("min_net_points") or 0),
            "min_pf": float(cfg.get("min_pf") if cfg.get("min_pf") is not None else 1.0)}


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
    p = _cfg_params(
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


# ---------- 切片判定 ----------
def _pf(gp: float, gl: float):
    """毛利/毛损 → PF; None=∞(无亏损有盈利), 0=没有盈利"""
    return round(gp / gl, 2) if gl > 0 else (None if gp > 0 else 0)


def _stat(n: int, gp: float, gl: float) -> dict:
    return {"n": n, "net": round(gp - gl, 1), "pf": _pf(gp, gl)}


async def _judge_symbol(pool, tls, bt, vid, p):
    """单品种切片判定: 逐笔按入场日贴指定版本时间线(与九币矩阵同一口径, points×mult 加权),
    判定窗 = 近 window_years 年。每刀 = 近 b 年(后段) vs 剩余(前段)。
    段内合格 = 笔数≥地板 且 净点>阈值 且 PF>阈值(无亏损段 PF=∞ 恒过)。
    返回三层读数: 整窗 total / 每格 cells_stat / 每切分前后段 splits_stat + 合格格。"""
    sym = bt["symbol"]
    tl = tls.get(sym)
    if tl is None:      # 切谁治谁: 先自愈指定版本的时间线
        try:
            await regime.ensure_timeline(pool, sym, vid)
        except Exception as e:
            logger.warning("regime ensure %s v%s failed: %s", sym, vid, e)
        tl = tls[sym] = await regime.tl_map(pool, sym, vid)
    win_start = bt["to_time"] - timedelta(days=p["window_years"] * 365.25)
    tagged, unlabeled, cnt = [], 0, 0
    for t in (bt["trades"] or []):
        if t["entry_time"] < win_start.timestamp():
            continue
        cnt += 1
        cell = tl.get(datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).date())
        if cell is None:
            unlabeled += 1
            continue
        tagged.append((t["entry_time"], cell,
                       float(t.get("points") or 0) * float(t.get("mult") or 1)))
    floor, min_net, min_pf = p["min_cell_trades"], p["min_net_points"], p["min_pf"]

    def _seg_ok(n, gp, gl):
        if n < floor or gp - gl <= min_net:
            return False
        return (gp / gl > min_pf) if gl > 0 else gp > 0   # 无亏损段 PF=∞

    # 整窗 + 每格: [n, 毛利, 毛损]
    tot, per_cell = [0, 0.0, 0.0], {}
    for ts, cell, net in tagged:
        for acc in (tot, per_cell.setdefault(cell, [0, 0.0, 0.0])):
            acc[0] += 1
            if net >= 0:
                acc[1] += net
            else:
                acc[2] -= net
    qual, splits, splits_stat = None, {}, {}
    for y in p["boundaries_years"]:
        cut_ts = (bt["to_time"] - timedelta(days=y * 365.25)).timestamp()
        seg: dict = {}      # 格 → [剩余段 n/毛利/毛损, 近段 n/毛利/毛损]
        for ts, cell, net in tagged:
            s = seg.setdefault(cell, [0, 0.0, 0.0, 0, 0.0, 0.0])
            o = 0 if ts < cut_ts else 3
            s[o] += 1
            if net >= 0:
                s[o + 1] += net
            else:
                s[o + 2] -= net
        ok = {c for c, v in seg.items()
              if _seg_ok(v[0], v[1], v[2]) and _seg_ok(v[3], v[4], v[5])}
        splits[f"{y:g}"] = sorted(ok)
        splits_stat[f"{y:g}"] = {c: {"f": _stat(v[0], v[1], v[2]),
                                     "b": _stat(v[3], v[4], v[5])} for c, v in seg.items()}
        qual = ok if qual is None else (qual & ok)
    return {"symbol": sym, "trades": cnt, "unlabeled": unlabeled,
            "splits": splits, "cells": sorted(qual or ()),
            "total": _stat(*tot),
            "cells_stat": {c: _stat(*v) for c, v in sorted(per_cell.items())},
            "splits_stat": splits_stat,
            "window": f"{max(bt['from_time'], win_start):%Y-%m-%d} ~ {bt['to_time']:%Y-%m-%d}"}


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
    """现跑现判(2026-08-04 Frank 定"不用现成的"): 每策略现跑总计年回测(结果 UPSERT 回流
    backtests, 假窗口旧行被真数据覆盖), 逐笔切片判稳健, 一跑一报告(点名诊断不入库)。"""
    pool = request.app.state.pool
    if req.mode not in ("preview", "execute"):
        raise HTTPException(status_code=400, detail="mode 需为 preview / execute")
    if req.symbols not in ("main", "all"):
        raise HTTPException(status_code=400, detail="symbols 需为 main / all")
    if req.ids and req.mode == "execute":
        raise HTTPException(status_code=400, detail="按 ID 点名 = 只读诊断, 只支持预览模式")

    p = _cfg_params(
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
                        await _judge_symbol(pool, tls, bt, vid, p)
                _progress["done"] += 1
    finally:
        _progress.update(running=False, current="")

    tag_ids, archive_ids = [], []
    for r in targets:
        d = {"id": r["id"], "name": r["name"], "symbol": r["symbol"], "status": r["status"]}
        res_map = judged.get(r["id"], {})
        main_res = res_map.get(r["symbol"])
        if isinstance(main_res, dict) and "error" in main_res:
            d.update(verdict="skip", reason=f"回测失败: {main_res['error']}")
            details.append(d)
            continue
        if main_res is None:
            d.update(verdict="skip",
                     reason=f"主品种 M1 覆盖不足总计 {p['window_years']:g} 年")
            details.append(d)
            continue
        results = [x for x in res_map.values()
                   if isinstance(x, dict) and "error" not in x and x is not None]
        # 明细三层读数: 窗口/笔数按判定窗口径; 整窗 total → 每格 → 每切分前后段 → 结论
        d.update(window=main_res["window"], trades=main_res["trades"],
                 splits=main_res["splits"], unlabeled=main_res["unlabeled"],
                 pass_cells=main_res["cells"], total=main_res["total"],
                 cells_stat=main_res["cells_stat"], splits_stat=main_res["splits_stat"])
        if req.symbols == "all":
            d["cross"] = [{"symbol": x["symbol"], "pass_cells": x["cells"],
                           "trades": x["trades"]} for x in results
                          if x["symbol"] != r["symbol"]]
        ok_list = [(x["symbol"], x["cells"]) for x in results
                   if len(x["cells"]) >= p["min_pass_cells"]]
        readonly = r["status"] != "CANDIDATE"   # 点名可带任意状态: 非空闲只读判定
        # 通过判定(2026-08-04 Frank 定): 主货币=主品种达标;
        # 全货币=任一品种存在合格格即过(发现型 — 如实列出哪个货币哪些格合格)
        ok = bool(ok_list) if req.symbols == "all" \
            else len(main_res["cells"]) >= p["min_pass_cells"]
        if ok:
            d.update(verdict="pass",
                     reason="合格: " + " · ".join(f"{s} {'·'.join(c)}" for s, c in ok_list)
                     + ("(非空闲, 只记录不打标签)" if readonly else ""))
            if not readonly:
                tag_ids.append(r["id"])
        else:
            d.update(verdict="fail",
                     reason=("各品种均无" if req.symbols == "all" else "无")
                     + f"合格格(全切分达标格 < {p['min_pass_cells']} 个)"
                     + ("(非空闲, 只记录不归档)" if readonly else ""))
            if not readonly:
                archive_ids.append(r["id"])
        details.append(d)

    summary = {"total": len(details),
               "passed": sum(1 for d in details if d["verdict"] == "pass"),
               "failed": sum(1 for d in details if d["verdict"] == "fail"),
               "archived": len(archive_ids) if req.mode == "execute" else 0,
               "skipped": sum(1 for d in details if d["verdict"] == "skip"),
               "not_run": not_run}
    # 点名 = 只读诊断连报告都不入库(结果直接返回页面看); regime_screens 只留全池清理履历
    rid = None
    if not req.ids:
        rid = await pool.fetchval(
            "INSERT INTO regime_screens"
            " (mode, version_id, scope, params, summary, details, owner_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            req.mode, vid, scope, p, summary, details,
            getattr(request.state, "user_id", None) or 1)   # 报告记发起人(schema/059)
        _progress["report_id"] = rid
    if req.mode == "execute":
        if tag_ids:   # 通过 → basis 追加标签(报告号可溯源到本次参数与全量明细)
            await pool.execute(
                "UPDATE strategies SET basis = CASE WHEN COALESCE(basis, '') = ''"
                " THEN $2 ELSE basis || '｜' || $2 END, updated_at = now()"
                " WHERE id = ANY($1)", tag_ids, f"{TAG}#{rid}")
        if archive_ids:   # 未过 → 归档(可逆; 条件重申 CANDIDATE 防运行间隙被切状态)
            await pool.execute(
                "UPDATE strategies SET status='ARCHIVED', archive_reason='regime_unstable',"
                " updated_at = now() WHERE id = ANY($1) AND status = 'CANDIDATE'",
                archive_ids)
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
async def screen_report(report_id: int, request: Request):
    """报告明细(结论级)。按需加载(2026-08-05 与 Frank 定): splits_stat 占 details 的 81%
    (500策略 2.5MB), 页面用不到就不传 — 剥掉后 ~590KB; 展开某策略时走
    /reports/{id}/strategy/{sid} 单取深层数字。库里 details 仍存全量(审计不缩水)"""
    row = await request.app.state.pool.fetchrow(
        "SELECT id, created_at, mode, version_id, scope, params, summary, owner_id,"
        "       (SELECT jsonb_agg(e - 'splits_stat') FROM jsonb_array_elements(details) e)"
        "         AS details"
        " FROM regime_screens WHERE id = $1", report_id)
    uid = identity.scope_uid(request)
    if row is None or (uid and row["owner_id"] != uid):   # 别人的报告 = 404 不暴露存在性
        raise HTTPException(status_code=404, detail="report not found")
    return {**{k: row[k] for k in row.keys() if k != "owner_id"},
            "created_at": row["created_at"].isoformat()}


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
