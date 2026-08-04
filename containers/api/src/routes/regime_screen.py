"""v0.5 Regime 筛选(批量粗筛 + 落库报告, 2026-08-03 与 Frank 定稿) — 测试性质可插拔功能。

漏斗关 1.5: 批量生成的策略 5 年回测后, 自动检验"regime 格子的时间稳健性" —
按年切 N 刀, 存在一个格子在【每种切分的前后两段】都(净点>0 ≡ PF>1, 且笔数≥地板)才通过。
执行动作只有两个系统本来就支持的写入(= 自动化的人在点按钮):
  通过 → basis 追加 ｜regime筛过#<报告id>    未过 → 归档(死因 regime_unstable, 可逆)
运行选项(2026-08-03 Frank 补): regime 版本(v1/v2..., 判定用哪套时间线) +
货币范围(main=只筛主货币 role=trade 的策略 / all=全部品种的策略)。
自包含: 判据参数读写(config regime_screen)也在本文件, 不占通用 /config PUT;
移除手册见 docs/2.regime_dirction/v0.5_regime筛选设计.md。
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import identity, regime

logger = logging.getLogger(__name__)
router = APIRouter()

TAG = "regime筛过"   # basis 标签词根: 已含即幂等跳过; 列表页搜"标签/生因"可一键捞幸存者


# ---------- 判据参数(config regime_screen 唯一源) ----------
class ScreenParams(BaseModel):
    boundaries_years: list[int]
    min_cell_trades: int


@router.post("/regime_screen/params")
async def screen_params_save(req: ScreenParams, request: Request):
    bs = sorted(set(req.boundaries_years))
    if not bs or any(y < 1 or y > 10 for y in bs):
        raise HTTPException(status_code=400, detail="切分边界需为 1~10 的整数(年)")
    if not 1 <= req.min_cell_trades <= 100:
        raise HTTPException(status_code=400, detail="格内最少笔数需在 1~100")
    await request.app.state.pool.execute(
        "INSERT INTO config (key, value) VALUES ('regime_screen', $1)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"boundaries_years": bs, "min_cell_trades": req.min_cell_trades})
    return {"boundaries_years": bs, "min_cell_trades": req.min_cell_trades}


def _scope_conds(req_ids, req_symbols, uid):
    """范围 → SQL 条件(plan 与 run 共用同一判据, 预估数 = 实跑数)。
    2026-08-04 Frank 定: 默认 = 全部未筛过的空闲策略(轮番清理, SQL 直接排掉已筛过 —
    否则全池报告会被历史 skip 行灌满); 按 ID 点名 = 显式小范围(已筛过的保留进报告记 skip)。
    symbols(main/all)不是范围过滤是判定数据源, 只记进 scope 供报告展示。"""
    conds, args = ["s.status = 'CANDIDATE'"], []
    if req_ids:
        args.append(req_ids)
        conds.append(f"s.id = ANY(${len(args)})")
        scope = {"ids": req_ids, "symbols": req_symbols}
    else:
        conds.append(f"COALESCE(s.basis, '') NOT LIKE '%{TAG}%'")
        scope = {"pool": "unscreened", "symbols": req_symbols}
    if uid:
        args.append(uid)
        conds.append(f"s.owner_id = ${len(args)}")
    return conds, args, scope


@router.get("/regime_screen/plan")
async def screen_plan(request: Request, ids: Optional[str] = None, symbols: str = "main"):
    """运行预估(页面预览行实时刷): 匹配多少 / 可判多少 / 各类跳过多少 — 纯读零动作。
    不传 ids = 默认范围(全部未筛过的空闲策略)"""
    pool = request.app.state.pool
    id_list = None
    if ids:
        try:
            id_list = [int(x) for x in ids.replace("，", ",").split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="ID 列表需为逗号分隔的整数")
    cfg = await pool.fetchval("SELECT value FROM config WHERE key='regime_screen'") or {}
    boundaries = sorted(cfg.get("boundaries_years") or [1, 2, 3, 4])
    need_days = int((max(boundaries) + 1) * 365.25) - 45
    conds, args, _ = _scope_conds(id_list, symbols, identity.scope_uid(request))
    coverage = None
    if symbols == "all":   # 全货币 = 判定用全部已回测品种 → 预览要看得见覆盖了哪些品种
        coverage = [{"symbol": r["symbol"], "n": r["n"]} for r in await pool.fetch(
            "SELECT b.symbol, count(DISTINCT b.strategy_id)::int AS n"
            "  FROM strategies s JOIN backtests b ON b.strategy_id = s.id"
            f" WHERE {' AND '.join(conds)} GROUP BY b.symbol ORDER BY b.symbol", *args)]
    args.append(need_days)
    nd = f"${len(args)}"
    row = await pool.fetchrow(
        "SELECT count(*)::int AS total,"
        "       count(*) FILTER (WHERE tagged)::int AS tagged,"
        "       count(*) FILTER (WHERE NOT tagged AND days IS NULL)::int AS no_backtest,"
        f"      count(*) FILTER (WHERE NOT tagged AND days < {nd})::int AS short_window,"
        f"      count(*) FILTER (WHERE NOT tagged AND days >= {nd})::int AS runnable"
        f" FROM (SELECT (COALESCE(s.basis, '') LIKE '%{TAG}%') AS tagged,"
        "              EXTRACT(epoch FROM (b.to_time - b.from_time)) / 86400 AS days"
        "         FROM strategies s"
        "         LEFT JOIN LATERAL (SELECT from_time, to_time FROM backtests"
        "               WHERE strategy_id = s.id AND symbol = s.symbol"
        "               ORDER BY created_at DESC LIMIT 1) b ON true"
        f"       WHERE {' AND '.join(conds)}) t", *args)
    out = {**dict(row), "need_days": need_days}
    if coverage is not None:
        out["coverage"] = coverage
    return out


# ---------- 运行(api 请求内直接算: 读 trades + 贴格, 轻活不派 worker) ----------
class ScreenRun(BaseModel):
    mode: str = "preview"              # preview=纯报告零动作 / execute=打标签+归档
    ids: Optional[list[int]] = None    # 按 ID 点名; 不传 = 全部未筛过的空闲策略(轮番清理)
    task: Optional[str] = None         # 任务标签(可选): 给这次清理起个名, 记进报告好认
    version: Optional[int] = None      # regime 版本, 不传 = 当前默认
    symbols: str = "main"              # 判定数据源: main=只用主品种回测行 / all=全部已回测品种都得过
    limit: Optional[int] = None        # 单次最多判多少个(超出的下次再跑), 不传 = 不限


async def _judge_symbol(pool, tls, bt, vid, boundaries, floor):
    """单品种判定: 逐笔按入场日贴指定版本时间线(与九币矩阵同一口径, points×mult 加权),
    返回各切分合格格与最终合格格(= 各切分交集)"""
    sym = bt["symbol"]
    tl = tls.get(sym)
    if tl is None:      # 切谁治谁: 先自愈指定版本的时间线
        try:
            await regime.ensure_timeline(pool, sym, vid)
        except Exception as e:
            logger.warning("regime ensure %s v%s failed: %s", sym, vid, e)
        tl = tls[sym] = await regime.tl_map(pool, sym, vid)
    tagged, unlabeled = [], 0
    for t in (bt["trades"] or []):
        cell = tl.get(datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).date())
        if cell is None:
            unlabeled += 1
            continue
        tagged.append((t["entry_time"], cell,
                       float(t.get("points") or 0) * float(t.get("mult") or 1)))
    qual, splits = None, {}
    for y in boundaries:
        cut_ts = (bt["to_time"] - timedelta(days=y * 365.25)).timestamp()
        seg: dict = {}      # 格 → [前段笔数, 前段净点, 后段笔数, 后段净点]
        for ts, cell, net in tagged:
            s = seg.setdefault(cell, [0, 0.0, 0, 0.0])
            i = 0 if ts < cut_ts else 2
            s[i] += 1
            s[i + 1] += net
        ok = {c for c, v in seg.items()
              if v[0] >= floor and v[1] > 0 and v[2] >= floor and v[3] > 0}
        splits[str(y)] = sorted(ok)
        qual = ok if qual is None else (qual & ok)
    return {"symbol": sym, "trades": len(bt["trades"] or []), "unlabeled": unlabeled,
            "splits": splits, "cells": sorted(qual or ()),
            "window": f"{bt['from_time']:%Y-%m-%d} ~ {bt['to_time']:%Y-%m-%d}"}


@router.post("/regime_screen/run")
async def screen_run(req: ScreenRun, request: Request):
    """一跑一报告(预览也落库, mode 区分)。只筛空闲(CANDIDATE)策略, 在跑的不进范围。
    货币选项 = 判定数据源: main=只用主品种回测行; all=该策略所有已回测品种每个独立判
    (各贴各的时间线), 全过才过(跨品种稳健; 窗口不足的跨品种行不纳入要求)。"""
    pool = request.app.state.pool
    if req.mode not in ("preview", "execute"):
        raise HTTPException(status_code=400, detail="mode 需为 preview / execute")
    if req.symbols not in ("main", "all"):
        raise HTTPException(status_code=400, detail="symbols 需为 main / all")

    cfg = await pool.fetchval("SELECT value FROM config WHERE key='regime_screen'") or {}
    boundaries = sorted(cfg.get("boundaries_years") or [1, 2, 3, 4])
    floor = int(cfg.get("min_cell_trades") or 5)
    # 窗口须容下最深一刀之外还有前段可判(默认四刀 = 约 5 年), 差 45 天容差
    need_days = int((max(boundaries) + 1) * 365.25) - 45

    if req.version is not None:
        if not await pool.fetchval("SELECT 1 FROM regime_versions WHERE id=$1", req.version):
            raise HTTPException(status_code=400, detail=f"regime 版本 v{req.version} 不存在")
        vid = int(req.version)
    else:
        vid, _ = await regime.active_version(pool)

    # 范围: 只筛空闲(CANDIDATE)策略 — 在跑(demo/live)/已归档不进范围(2026-08-03 Frank 定,
    # 不做特殊分支); 已筛过的拉回来记 skip(汇总要看得见幂等跳过了多少)
    conds, args, scope = _scope_conds(req.ids, req.symbols, identity.scope_uid(request))
    if req.limit:
        scope["limit"] = req.limit
    if (req.task or "").strip():
        scope["task"] = req.task.strip()
    rows = await pool.fetch(
        f"SELECT s.id, s.name, s.symbol, s.basis, s.status FROM strategies s"
        f" WHERE {' AND '.join(conds)} ORDER BY s.id", *args)
    if not rows:
        raise HTTPException(status_code=404, detail=(
            "点名的 ID 里没有空闲策略" if req.ids else "没有未筛过的空闲策略 — 池子已清完"))

    tls: dict = {}                      # 品种时间线缓存(同一 vid, 按品种存)
    details, tag_ids, archive_ids, not_run = [], [], [], 0
    for idx, r in enumerate(rows):
        # 单次上限按"实际判定数"计(跳过不占额度); 剩下的不进本报告, 下次再跑
        if req.limit and len(tag_ids) + len(archive_ids) >= req.limit:
            not_run = len(rows) - idx
            break
        d = {"id": r["id"], "name": r["name"], "symbol": r["symbol"], "status": r["status"]}
        if r["basis"] and TAG in r["basis"]:
            d.update(verdict="skip", reason="已筛过(幂等不重复)")
            details.append(d)
            continue
        # 判定数据源: main=主品种最新一行; all=每品种最新一行(跨品种回测由回测页产出)
        if req.symbols == "main":
            bt = await pool.fetchrow(
                "SELECT symbol, from_time, to_time, trades FROM backtests"
                " WHERE strategy_id = $1 AND symbol = $2"
                " ORDER BY created_at DESC LIMIT 1", r["id"], r["symbol"])
            bts = [bt] if bt else []
        else:
            bts = await pool.fetch(
                "SELECT DISTINCT ON (symbol) symbol, from_time, to_time, trades"
                " FROM backtests WHERE strategy_id = $1 ORDER BY symbol, created_at DESC",
                r["id"])
        main_bt = next((b for b in bts if b["symbol"] == r["symbol"]), None)
        if main_bt is None:
            d.update(verdict="skip", reason="无主品种回测")
            details.append(d)
            continue
        main_days = (main_bt["to_time"] - main_bt["from_time"]).days
        d["window"] = f"{main_bt['from_time']:%Y-%m-%d} ~ {main_bt['to_time']:%Y-%m-%d}"
        d["trades"] = len(main_bt["trades"] or [])
        if main_days < need_days:
            d.update(verdict="skip", reason=f"窗口不足(需≥{need_days}天, 实{main_days}天)")
            details.append(d)
            continue
        # 逐品种判定: 主品种必判; 跨品种(all)窗口不足的不纳入要求(宁缺毋滥单向: 只加码不放水)
        results = []
        for b in bts:
            if b["symbol"] != r["symbol"] and (b["to_time"] - b["from_time"]).days < need_days:
                continue
            results.append(await _judge_symbol(pool, tls, b, vid, boundaries, floor))
        main_res = next(x for x in results if x["symbol"] == r["symbol"])
        d.update(splits=main_res["splits"], unlabeled=main_res["unlabeled"],
                 pass_cells=main_res["cells"])
        if req.symbols == "all":
            d["cross"] = [{"symbol": x["symbol"], "pass_cells": x["cells"],
                           "trades": x["trades"]} for x in results
                          if x["symbol"] != r["symbol"]]
        fail_syms = [x["symbol"] for x in results if not x["cells"]]
        if not fail_syms:
            d.update(verdict="pass", reason="合格格 " + "·".join(main_res["cells"])
                     + (f" · 跨品种 {len(results) - 1} 个全过" if len(results) > 1 else ""))
            tag_ids.append(r["id"])
        else:
            d.update(verdict="fail", reason="未过品种: " + "·".join(fail_syms)
                     + "(无一格在全部切分前后段都达标)")
            archive_ids.append(r["id"])
        details.append(d)

    summary = {"total": len(details), "passed": len(tag_ids),
               "failed": sum(1 for d in details if d["verdict"] == "fail"),
               "archived": len(archive_ids) if req.mode == "execute" else 0,
               "skipped": sum(1 for d in details if d["verdict"] == "skip"),
               "not_run": not_run}
    rid = await pool.fetchval(
        "INSERT INTO regime_screens (mode, version_id, scope, params, summary, details)"
        " VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
        req.mode, vid, scope,
        {"boundaries_years": boundaries, "min_cell_trades": floor}, summary, details)
    if req.mode == "execute":
        if tag_ids:   # 通过 → basis 追加标签(报告号可溯源到本次参数与全量明细)
            await pool.execute(
                "UPDATE strategies SET basis = CASE WHEN COALESCE(basis, '') = ''"
                " THEN $2 ELSE basis || '｜' || $2 END, updated_at = now()"
                " WHERE id = ANY($1)", tag_ids, f"{TAG}#{rid}")
        if archive_ids:   # 未过 → 归档(可逆; 条件重申 CANDIDATE 防运行间隙被切状态)
            await pool.execute(
                "UPDATE strategies SET status='ARCHIVED', archive_reason='regime_unstable',"
                " updated_at = now() WHERE id = ANY($1) AND status = 'CANDIDATE'", archive_ids)
    return {"report_id": rid, "mode": req.mode, "version": vid,
            "summary": summary, "details": details}


# ---------- 报告回看 ----------
@router.get("/regime_screen/reports")
async def screen_reports(request: Request, limit: int = 30):
    rows = await request.app.state.pool.fetch(
        "SELECT id, created_at, mode, version_id, scope, params, summary"
        " FROM regime_screens ORDER BY id DESC LIMIT $1", min(max(limit, 1), 200))
    return {"reports": [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]}


@router.get("/regime_screen/reports/{report_id}")
async def screen_report(report_id: int, request: Request):
    row = await request.app.state.pool.fetchrow(
        "SELECT id, created_at, mode, version_id, scope, params, summary, details"
        " FROM regime_screens WHERE id = $1", report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return {**dict(row), "created_at": row["created_at"].isoformat()}
