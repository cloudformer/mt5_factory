"""筛选·策略×regime 映射规律(2026-08-11 与 Frank 定稿) — 批量筛子。

问一件事: 这个策略的盈亏能不能被某个 regime 口径的八个格分出层次?
交易按【出场原因】分四类(止盈/止损/跳空有利/跳空不利) → 每版本【独立】做 4×8 列联表 → 置换检验。

铁律: 各版本独立评估, 【绝不跨版本比较/排名】—— 不同版本的同名格是不同的分类维度。
纯计算不跑引擎(复用 backtests.trades), 走 jobs 队列并行; 一块 = 一批策略。
插件式可移除: 删本文件 + app.py 两行 + web 三件套 + DROP TABLE regime_map_screens。
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import identity, jobs, regime_map

logger = logging.getLogger("regime_map_api")
router = APIRouter()
LOCK_KEY = 70111811          # advisory lock: 收尾串行(与 oos_v2/screen 各用各的)
CHUNK = 10                   # 一块几个策略: 置换检验较慢, 块小=进度细+并行足


class RunRequest(BaseModel):
    # 判据(2026-08-11 Frank 定: 走页面表单不落 config, 每次现填, 随报告存快照)
    # 四类 = 出场原因(止盈/锁利 · 正常止损 · 跳空有利 · 跳空不利), 无需口径参数
    permutations: int = 1000      # 置换次数(探索期 1000 够快)
    sig_p: float = 0.05           # 显著性门槛
    min_enrich: float = 1.5       # 富集倍数门槛
    min_cell_trades: int = 30     # 该格总笔数门槛(格本身不能是碎格)
    min_tier_cell: int = 10       # 该类在该格的笔数门槛(3 笔算出的 3.77x 是噪音不是信号)
    min_tier_pct: float = 10.0    # 样本不足提示线(合计占比%): 低于此值的类只提示不判定; 0=关
    # 范围

    ids: Optional[list[int]] = None       # 点名; 空 = 按筛选条件全池
    # 批次(2026-08-12 Frank 要, 与回测页同名同义): 策略生成时填的标签, 存 strategies.basis。
    # 一次实验一个批次 → 分开读 M15/H4/H1 三组的关联差异; 混池会把要看的差异抹平
    basis: Optional[str] = None
    template: Optional[str] = None
    symbol: Optional[str] = None
    status: Optional[str] = None
    limit: Optional[int] = None
    task: Optional[str] = None


@router.post("/regime_map/run")
async def run(req: RunRequest, request: Request):
    """投递批量任务: 按范围圈策略 → 切块投 jobs(kind=regime_map) → worker 并行算。
    只读 backtests.trades(7天内的回测直接复用, 不重跑引擎)。"""
    pool = request.app.state.pool
    if not 0 < req.sig_p < 1:
        raise HTTPException(status_code=400, detail="sig_p 须在 (0,1)")
    if not 100 <= req.permutations <= 20000:
        raise HTTPException(status_code=400, detail="置换次数须 100~20000")
    p = regime_map.cfg_params(req.model_dump())     # 判据规范化(带默认), 随报告存快照
    uid = identity.scope_uid(request)
    conds, args = ["EXISTS (SELECT 1 FROM backtests b WHERE b.strategy_id = s.id"
                   "         AND b.symbol = s.symbol)"], []
    if req.ids:
        args.append(req.ids)
        conds.append(f"s.id = ANY(${len(args)})")
    else:
        for col, val in (("template", req.template), ("symbol", req.symbol),
                         ("status", req.status), ("basis", req.basis)):
            if val:
                args.append(val)
                conds.append(f"s.{col} = ${len(args)}")
        conds.append("s.status <> 'ARCHIVED'")
    if uid:
        args.append(uid)
        conds.append(f"s.owner_id = ${len(args)}")
    limit = min(int(req.limit or 200), 5000)
    rows = await pool.fetch(
        f"SELECT s.id FROM strategies s WHERE {' AND '.join(conds)}"
        f" ORDER BY s.id LIMIT {limit}", *args)
    ids = [r["id"] for r in rows]
    if not ids:
        raise HTTPException(status_code=400, detail="范围内没有【已有回测行】的策略")
    versions = [r["id"] for r in await pool.fetch(
        "SELECT id FROM regime_versions ORDER BY id")]
    if not versions:
        raise HTTPException(status_code=400, detail="没有 regime 版本")
    await pool.execute("DELETE FROM jobs WHERE kind=$1", jobs.MAP_KIND)
    scope = {"task": (req.task or "").strip() or None, "ids": req.ids,
             "basis": req.basis,
             "template": req.template, "symbol": req.symbol, "status": req.status,
             "limit": limit, "count": len(ids)}
    items = [{"ids": ids[i:i + CHUNK], "versions": versions, "params": p, "scope": scope}
             for i in range(0, len(ids), CHUNK)]
    await jobs.submit_batch(pool, items, kind=jobs.MAP_KIND)
    logger.info("regime_map run: %d strategies × %d versions → %d chunks",
                len(ids), len(versions), len(items))
    return {"started": True, "strategies": len(ids), "versions": versions,
            "chunks": len(items)}


@router.get("/regime_map/progress")
async def progress(request: Request):
    row = await request.app.state.pool.fetchrow(
        "SELECT count(*)::int AS total,"
        "       count(*) FILTER (WHERE status IN ('DONE','FAILED'))::int AS done"
        "  FROM jobs WHERE kind=$1", jobs.MAP_KIND)
    return {"running": bool(row["total"] and row["done"] < row["total"]),
            "total": row["total"], "done": row["done"]}


def _pool(details: list) -> dict:
    """跨策略池化(2026-08-13 与 Frank 定) —— 单策略读不出的东西, 池化才有。

    为什么必须池化: H4 单策略只有 261 笔, 八格分下来每格 30 笔, 4 个百分点的差根本测不出;
    H1 单策略 3688 笔够, 但那是【一个策略】的运气, 我们要问的是"这规律普遍成立吗"。
    分桶: 全部 / 各周期 / 各 slow(持仓长短的代理) / 各品种 —— 换品种重现比任何 p 值都硬。
    【按版本各池各的】: 铁律 = 版本之间不可比(同名格是不同分类维度), 绝不合并、绝不排名。
    """
    out = {}
    for d in details:
        for vid, v in (d.get("versions") or {}).items():
            sp = v.get("splits")
            if not sp:
                continue
            for kind, key in (("all", "全部"), ("tf", d.get("timeframe")),
                              ("slow", d.get("slow")), ("sym", d.get("symbol"))):
                if key is None:
                    continue
                box = out.setdefault(vid, {}).setdefault(kind, {}).setdefault(str(key), {})
                for grp, sides in sp.items():
                    for side, (n, tp) in sides.items():
                        a = box.setdefault(grp, {}).setdefault(side, [0, 0])
                        a[0] += n
                        a[1] += tp
    # 汇总成可读数: 每桶每分法给 两边的笔数/止盈率 + 差(百分点)
    res = {}
    for vid, kinds in out.items():
        for kind, boxes in kinds.items():
            for key, groups in boxes.items():
                for grp, sides in groups.items():
                    r = {}
                    for side, (n, tp) in sides.items():
                        r[side] = {"n": n, "win_pct": round(tp / n * 100, 1) if n else None}
                    a, b = (("diverge", "align") if grp == "trend" else ("A", "B"))
                    ra, rb = r.get(a), r.get(b)
                    r["diff"] = (round(ra["win_pct"] - rb["win_pct"], 1)
                                 if ra and rb and ra["win_pct"] is not None
                                 and rb["win_pct"] is not None else None)
                    res.setdefault(vid, {}).setdefault(kind, {}).setdefault(key, {})[grp] = r
    return res


async def finalize(pool) -> int | None:
    """收尾: 全部块跑完 → 合并 result → 落报告 → 删队列(单事务, 失败整体回滚防复读)"""
    rows = await pool.fetch(
        "SELECT id, status, payload, result FROM jobs WHERE kind=$1 ORDER BY id",
        jobs.MAP_KIND)
    if not rows or any(r["status"] in ("PENDING", "RUNNING") for r in rows):
        return None
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
            return None
        try:
            if not await conn.fetchval("SELECT count(*) FROM jobs WHERE kind=$1",
                                       jobs.MAP_KIND):
                return None
            details = []
            for r in rows:
                if r["status"] == "DONE" and r["result"]:
                    details.extend(r["result"])
                else:
                    details.extend({"id": i, "verdict": "skip", "reason": "计算块失败"}
                                   for i in (r["payload"] or {}).get("ids", []))
            details.sort(key=lambda d: d.get("id", 0))
            # 池化读数(2026-08-13): 报告顶上那块 + 历史列表那一列都吃它
            pooled = _pool(details)
            summary = {"pooled": pooled, "total": len(details),
                       "signal": sum(1 for d in details if d.get("verdict") == "signal"),
                       "weak": sum(1 for d in details if d.get("verdict") == "weak"),
                       "none": sum(1 for d in details if d.get("verdict") == "none"),
                       "skipped": sum(1 for d in details if d.get("verdict") == "skip")}
            p0 = rows[0]["payload"] or {}
            async with conn.transaction():
                rid = await conn.fetchval(
                    "INSERT INTO regime_map_screens (scope, params, summary, details)"
                    " VALUES ($1, $2, $3, $4) RETURNING id",
                    p0.get("scope") or {}, p0.get("params") or {}, summary, details)
                await conn.execute("DELETE FROM jobs WHERE kind=$1", jobs.MAP_KIND)
            logger.info("regime_map#%s done: %s", rid, summary)
            return rid
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)


@router.get("/regime_map/reports")
async def reports(request: Request, page: int = 1, per: int = 30):
    """报告列表(2026-08-12 加分页): 页面把列表摆在报告内容【上方】, 换报告不用滚到底"""
    pool = request.app.state.pool
    per = min(max(per, 1), 200)
    page = max(page, 1)
    total = await pool.fetchval("SELECT count(*)::int FROM regime_map_screens")
    rows = await pool.fetch(
        "SELECT id, created_at, scope, params, summary FROM regime_map_screens"
        " ORDER BY id DESC LIMIT $1 OFFSET $2", per, (page - 1) * per)
    return {"reports": [dict(r) for r in rows], "total": total,
            "page": page, "per": per,
            "pages": max((total + per - 1) // per, 1)}


@router.get("/regime_map/reports/{rid}")
async def report(rid: int, request: Request, verdict: Optional[str] = None,
                 page: int = 1, per: int = 50):
    r = await request.app.state.pool.fetchrow(
        "SELECT * FROM regime_map_screens WHERE id=$1", rid)
    if r is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    d = dict(r)
    items = d["details"]
    if verdict:
        items = [x for x in items if x.get("verdict") == verdict]
    total = len(items)
    d["details"] = items[(page - 1) * per: page * per]
    d["page"], d["per"], d["filtered"] = page, per, total
    return d
