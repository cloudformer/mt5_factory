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


# ---------- 判据参数(config regime_screen 唯一源; 切分 = 近 b 年 vs 剩余, b 可小数) ----------
class ScreenParams(BaseModel):
    window_years: float = 5            # 总计回测窗口(年): 窗口不足此数 = 跳过不判
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
    p = _cfg_params(
        await pool.fetchval("SELECT value FROM config WHERE key='regime_screen'") or {})
    need_days = int(p["window_years"] * 365.25) - 45
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


async def _judge_symbol(pool, tls, bt, vid, p):
    """单品种判定: 逐笔按入场日贴指定版本时间线(与九币矩阵同一口径, points×mult 加权),
    每刀 = 近 b 年(后段) vs 剩余(前段), 返回各切分合格格与最终合格格(= 各切分交集)。
    段内合格 = 笔数≥地板 且 净点>阈值 且 PF>阈值(无亏损段 PF=∞ 恒过)"""
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
    floor, min_net, min_pf = p["min_cell_trades"], p["min_net_points"], p["min_pf"]

    def _seg_ok(n, gp, gl):
        if n < floor or gp - gl <= min_net:
            return False
        return (gp / gl > min_pf) if gl > 0 else gp > 0   # 无亏损段 PF=∞

    qual, splits = None, {}
    for y in p["boundaries_years"]:
        cut_ts = (bt["to_time"] - timedelta(days=y * 365.25)).timestamp()
        seg: dict = {}      # 格 → [前段 n/毛利/毛损, 后段 n/毛利/毛损]
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

    p = _cfg_params(
        await pool.fetchval("SELECT value FROM config WHERE key='regime_screen'") or {})
    # 窗口须达总计年(差 45 天容差); 切分点超窗的丢弃(参数校验兜底)
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
            results.append(await _judge_symbol(pool, tls, b, vid, p))
        main_res = next(x for x in results if x["symbol"] == r["symbol"])
        d.update(splits=main_res["splits"], unlabeled=main_res["unlabeled"],
                 pass_cells=main_res["cells"])
        if req.symbols == "all":
            d["cross"] = [{"symbol": x["symbol"], "pass_cells": x["cells"],
                           "trades": x["trades"]} for x in results
                          if x["symbol"] != r["symbol"]]
        fail_syms = [x["symbol"] for x in results
                     if len(x["cells"]) < p["min_pass_cells"]]
        if not fail_syms:
            d.update(verdict="pass", reason="合格格 " + "·".join(main_res["cells"])
                     + (f" · 跨品种 {len(results) - 1} 个全过" if len(results) > 1 else ""))
            tag_ids.append(r["id"])
        else:
            d.update(verdict="fail", reason="未过品种: " + "·".join(fail_syms)
                     + f"(全切分达标格 < {p['min_pass_cells']} 个)")
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
        req.mode, vid, scope, p, summary, details)
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
