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


def _scope_conds(req_label, req_ids, req_symbols, uid):
    """范围 → SQL 条件(plan 与 run 共用同一判据, 预估数 = 实跑数)"""
    conds, args = ["s.status = 'CANDIDATE'"], []
    if req_ids:
        args.append(req_ids)
        conds.append(f"s.id = ANY(${len(args)})")
        scope = {"ids": req_ids, "symbols": req_symbols}
    else:
        args.append(f"%{req_label.strip()}%")
        conds.append(f"s.basis ILIKE ${len(args)}")
        scope = {"label": req_label.strip(), "symbols": req_symbols}
    if req_symbols == "main":
        conds.append("s.symbol IN (SELECT symbol FROM symbols WHERE role = 'trade')")
    if uid:
        args.append(uid)
        conds.append(f"s.owner_id = ${len(args)}")
    return conds, args, scope


@router.get("/regime_screen/plan")
async def screen_plan(request: Request, label: Optional[str] = None,
                      ids: Optional[str] = None, symbols: str = "main"):
    """运行预估(页面预览行实时刷): 匹配多少 / 可判多少 / 各类跳过多少 — 纯读零动作"""
    pool = request.app.state.pool
    id_list = None
    if ids:
        try:
            id_list = [int(x) for x in ids.replace("，", ",").split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="ID 列表需为逗号分隔的整数")
    if not id_list and not (label or "").strip():
        raise HTTPException(status_code=400, detail="需要范围: 批次标签 或 ID 列表")
    cfg = await pool.fetchval("SELECT value FROM config WHERE key='regime_screen'") or {}
    boundaries = sorted(cfg.get("boundaries_years") or [1, 2, 3, 4])
    need_days = int((max(boundaries) + 1) * 365.25) - 45
    conds, args, _ = _scope_conds(label, id_list, symbols, identity.scope_uid(request))
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
    return {**dict(row), "need_days": need_days}


# ---------- 运行(api 请求内直接算: 读 trades + 贴格, 轻活不派 worker) ----------
class ScreenRun(BaseModel):
    mode: str = "preview"              # preview=纯报告零动作 / execute=打标签+归档
    label: Optional[str] = None        # 范围: 批次标签(basis 模糊匹配)
    ids: Optional[list[int]] = None    # 范围: 明确 ID 列表(填了优先)
    version: Optional[int] = None      # regime 版本, 不传 = 当前默认
    symbols: str = "main"              # main=只筛主货币(role=trade)策略 / all=全部品种
    limit: Optional[int] = None        # 单次最多判多少个(超出的下次再跑), 不传 = 不限


@router.post("/regime_screen/run")
async def screen_run(req: ScreenRun, request: Request):
    """一跑一报告(预览也落库, mode 区分)。逐笔按入场日贴指定版本时间线 —
    与九币矩阵同一口径(points×mult 加权净点)。只筛空闲(CANDIDATE)策略, 在跑的不进范围。"""
    pool = request.app.state.pool
    if req.mode not in ("preview", "execute"):
        raise HTTPException(status_code=400, detail="mode 需为 preview / execute")
    if req.symbols not in ("main", "all"):
        raise HTTPException(status_code=400, detail="symbols 需为 main / all")
    if not req.ids and not (req.label or "").strip():
        raise HTTPException(status_code=400, detail="需要范围: 批次标签 或 ID 列表")

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
    conds, args, scope = _scope_conds(req.label, req.ids, req.symbols,
                                      identity.scope_uid(request))
    if req.limit:
        scope["limit"] = req.limit
    rows = await pool.fetch(
        "SELECT s.id, s.name, s.symbol, s.basis, s.status,"
        "       b.from_time, b.to_time, b.trades"
        "  FROM strategies s"
        "  LEFT JOIN LATERAL (SELECT from_time, to_time, trades FROM backtests"
        "        WHERE strategy_id = s.id AND symbol = s.symbol"
        "        ORDER BY created_at DESC LIMIT 1) b ON true"
        f" WHERE {' AND '.join(conds)} ORDER BY s.id", *args)
    if not rows:
        raise HTTPException(status_code=404, detail="范围内没有策略(或都已归档)")

    tls: dict = {}                      # 品种时间线缓存(切谁治谁: 先自愈指定版本)
    details, tag_ids, archive_ids, not_run = [], [], [], 0
    for idx, r in enumerate(rows):
        # 单次上限按"实际判定数"计(跳过不占额度); 剩下的不进本报告, 下次再跑
        if req.limit and len(tag_ids) + len(archive_ids) >= req.limit:
            not_run = len(rows) - idx
            break
        d = {"id": r["id"], "name": r["name"], "symbol": r["symbol"], "status": r["status"]}
        if r["basis"] and TAG in r["basis"]:
            d.update(verdict="skip", reason="已筛过(幂等不重复)")
        elif r["from_time"] is None:
            d.update(verdict="skip", reason="无本品种回测")
        else:
            window_days = (r["to_time"] - r["from_time"]).days
            d["window"] = f"{r['from_time']:%Y-%m-%d} ~ {r['to_time']:%Y-%m-%d}"
            trades = r["trades"] or []
            d["trades"] = len(trades)
            if window_days < need_days:
                d.update(verdict="skip", reason=f"窗口不足(需≥{need_days}天, 实{window_days}天)")
            else:
                tl = tls.get(r["symbol"])
                if tl is None:
                    try:
                        await regime.ensure_timeline(pool, r["symbol"], vid)
                    except Exception as e:
                        logger.warning("regime ensure %s v%s failed: %s", r["symbol"], vid, e)
                    tl = tls[r["symbol"]] = await regime.tl_map(pool, r["symbol"], vid)
                # 预贴格一次: (入场时间戳, 格, points×mult 加权净点)
                tagged, unlabeled = [], 0
                for t in trades:
                    cell = tl.get(datetime.fromtimestamp(
                        t["entry_time"], tz=timezone.utc).date())
                    if cell is None:
                        unlabeled += 1
                        continue
                    tagged.append((t["entry_time"], cell,
                                   float(t.get("points") or 0) * float(t.get("mult") or 1)))
                qual, splits = None, {}
                for y in boundaries:
                    cut_ts = (r["to_time"] - timedelta(days=y * 365.25)).timestamp()
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
                cells = sorted(qual or ())
                d.update(splits=splits, unlabeled=unlabeled, pass_cells=cells)
                if cells:
                    d.update(verdict="pass", reason=f"合格格 {'·'.join(cells)}")
                    tag_ids.append(r["id"])
                else:
                    d.update(verdict="fail", reason="无一格在全部切分前后段都达标")
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
