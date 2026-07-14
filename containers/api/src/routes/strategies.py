"""/strategies — 策略实例的生成与生命周期 + MQ5 转化流水线

职责: 模板清单、批量生成(grid/random/ai)、列表筛选、状态流转(准入漏斗)、
     MQ5 提交与跟踪(评估→翻译→纳入)。
策略逻辑本体在 strategy_core/ (回测与 Windows runner 共用同一份)。

扩展点:
- 新策略模板 = strategy_core/templates/ 加文件 + 注册 TEMPLATES (本文件不用改)
- AI 生成器 = 实现 POST {ai_generator_url}/propose 协议 (见 _ai_combos),
  在 web 下载页/config 表配置 ai_generator_url 即接入, 默认不配置走 random
"""
import asyncio
import logging
import random
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from src.services import backtest, verify
from strategy_core import TEMPLATES, TF_SECONDS, grid_combos, random_combo

logger = logging.getLogger("strategies")
router = APIRouter()


@router.get("/strategies/templates")
async def templates_list():
    """可用策略模板: 参数网格 + 随机采样空间 + 模板说明(模块 docstring) — 生成页展示定义用"""
    import sys
    return {"templates": {
        name: {
            "grid": cls.PARAM_GRID,
            "random": cls.RANDOM_SPACE,
            "doc": (sys.modules[cls.__module__].__doc__ or cls.__doc__ or "").strip(),
        } for name, cls in TEMPLATES.items()}}


class GenerateRequest(BaseModel):
    template: str
    symbols: list[str]
    timeframe: str = "M15"
    mode: str = "random"  # grid=固定网格(有限) | random=随机采样(默认) | ai=外部AI生成器
    count: int = 50       # random/ai 模式下每个品种生成的数量


async def _ai_combos(pool, template: str, symbol: str, timeframe: str, count: int) -> list[dict]:
    """调外部 AI 生成器取参数组合。协议:
    POST {ai_generator_url}/propose
      请求: {template, symbol, timeframe, count, param_space}
      响应: {"combos": [{参数dict}, ...]}
    返回的组合会经过 valid_params + 参数键校验, 非法的丢弃。"""
    url = await pool.fetchval("SELECT value FROM config WHERE key='ai_generator_url'")
    if not url:
        raise HTTPException(status_code=400,
                            detail="ai_generator_url 未配置 (config 表), AI 模式不可用; 可先用 random")
    cls = TEMPLATES[template]
    space = cls.RANDOM_SPACE or cls.PARAM_GRID
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(f"{str(url).rstrip('/')}/propose", json={
                "template": template, "symbol": symbol, "timeframe": timeframe,
                "count": count, "param_space": space,
            })
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"AI generator 不可达: {e}")
    keys = set(space)
    combos = []
    for params in r.json().get("combos", []):
        if isinstance(params, dict) and set(params) == keys and cls.valid_params(params):
            combos.append(params)
    return combos


async def _insert_instance(pool, template, symbol, timeframe, params) -> int:
    """写入一个策略实例; 重复组合返回 0 (唯一约束去重)"""
    name = f"{template}-{symbol}-{timeframe}-" + "-".join(
        f"{k}{params[k]}" for k in sorted(params))
    result = await pool.execute(
        "INSERT INTO strategies (name, template, symbol, timeframe, params)"
        " VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
        name, template, symbol, timeframe, params)
    return int(result.split()[-1])


@router.post("/strategies/generate")
async def generate(req: GenerateRequest, request: Request):
    """批量生成 CANDIDATE 实例 (重复组合自动跳过)"""
    if req.template not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"unknown template, available: {list(TEMPLATES)}")
    if req.timeframe not in TF_SECONDS:
        raise HTTPException(status_code=400, detail=f"invalid timeframe, available: {list(TF_SECONDS)}")
    if req.mode not in ("grid", "random", "ai"):
        raise HTTPException(status_code=400, detail="mode must be grid, random or ai")

    pool = request.app.state.pool
    # 品种唯一数据源: 只能给已登记(经券商校验)的品种生成策略, 根治"给券商没有的品种生成"
    symbols = [s.strip().upper() for s in req.symbols]
    known = {r["symbol"] for r in await pool.fetch(
        "SELECT symbol FROM symbols WHERE symbol = ANY($1::text[])", symbols)}
    unknown = [s for s in symbols if s not in known]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"品种未登记: {', '.join(unknown)} — 先在下载页登记(会向券商校验)再生成策略")
    created, total = 0, 0
    rng = random.Random()
    for symbol in symbols:
        if req.mode == "grid":
            for params in grid_combos(req.template):
                total += 1
                created += await _insert_instance(pool, req.template, symbol, req.timeframe, params)
        elif req.mode == "ai":
            for params in await _ai_combos(pool, req.template, symbol, req.timeframe, req.count):
                total += 1
                created += await _insert_instance(pool, req.template, symbol, req.timeframe, params)
        else:  # random: 多抽一些抵消撞重, 直到凑够 count 个新实例
            made = 0
            for _ in range(req.count * 5):  # 上限防死循环
                if made >= req.count:
                    break
                params = random_combo(req.template, rng)
                if params is None:
                    break
                total += 1
                n = await _insert_instance(pool, req.template, symbol, req.timeframe, params)
                created += n
                made += n
    logger.info("generated %d strategies (%s, mode=%s)", created, req.template, req.mode)
    return {"created": created, "skipped": total - created, "mode": req.mode,
            "template": req.template, "symbols": req.symbols}


@router.get("/strategies/status")
async def list_strategies(request: Request, status: Optional[str] = None,
                          symbol: Optional[str] = None, limit: int = 100):
    """策略实例列表, 按状态/品种筛选 (Windows runner 拉任务也走这里)。
    随附三方战绩 — web 页并排对比"回测质量 / demo / live 在券商是否一致":
      backtest: 最新一次回测指标 (backtests 表, 可能为 null)
      stats:    {"demo": {trades,wins,profit}, "live": {...}} (strategy_stats 表, 心跳快照)"""
    q = ("SELECT s.id, s.name, s.template, s.symbol, s.timeframe, s.params, s.status,"
         "       s.magic_number, sy.broker, b.metrics AS backtest, st.stats"
         "  FROM strategies s"
         "  LEFT JOIN symbols sy ON sy.symbol = s.symbol"  # 券商(来自品种主档)
         # 只取主品种回测 (symbol=s.symbol): 跨品种验证会写多品种行, 不能串到别品种成绩
         "  LEFT JOIN LATERAL (SELECT metrics FROM backtests"
         "                      WHERE strategy_id = s.id AND symbol = s.symbol"
         "                      ORDER BY id DESC LIMIT 1) b ON true"
         "  LEFT JOIN LATERAL (SELECT jsonb_object_agg(lower(env), jsonb_build_object("
         "                       'trades', trades, 'wins', wins,"
         "                       'profit', round(profit::numeric, 2))) AS stats"
         "                       FROM strategy_stats WHERE strategy_id = s.id) st ON true")
    cond, args = [], []
    if status:
        args.append(status); cond.append(f"s.status = ${len(args)}")
    if symbol:
        args.append(symbol); cond.append(f"s.symbol = ${len(args)}")
    if cond:
        q += " WHERE " + " AND ".join(cond)
    args.append(limit)
    q += f" ORDER BY s.id LIMIT ${len(args)}"
    rows = await request.app.state.pool.fetch(q, *args)
    return {"count": len(rows), "strategies": [dict(r) for r in rows]}


# ---------- MQ5 转化流水线 ----------
class Mq5Submit(BaseModel):
    name: str
    source: str            # .mq5 源码
    params_set: str = ""   # .set 参数(可选)


@router.post("/strategies/mq5")
async def mq5_submit(req: Mq5Submit, request: Request):
    """提交外部 MQ5 待评估 (评估/翻译走开发流程, 结论回写状态)"""
    if not req.source.strip():
        raise HTTPException(status_code=400, detail="source 不能为空")
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO mq5_imports (name, source, params_set) VALUES ($1, $2, $3)"
        " RETURNING id, name, status, created_at",
        req.name.strip(), req.source, req.params_set or None)
    return dict(row)


@router.get("/strategies/mq5")
async def mq5_list(request: Request):
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, status, assessment, template, consistency, length(source) AS source_bytes,"
        "       created_at, updated_at FROM mq5_imports ORDER BY id DESC")
    return {"imports": [dict(r) for r in rows]}


@router.get("/strategies/mq5/{import_id}")
async def mq5_detail(import_id: int, request: Request):
    row = await request.app.state.pool.fetchrow(
        "SELECT * FROM mq5_imports WHERE id=$1", import_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return dict(row)


class Mq5Update(BaseModel):
    status: str | None = None       # ASSESSED | TRANSLATED | REJECTED
    assessment: str | None = None
    template: str | None = None     # 翻译完成后指向 strategy_core 模板名


@router.patch("/strategies/mq5/{import_id}")
async def mq5_update(import_id: int, req: Mq5Update, request: Request):
    """回写评估结论/翻译结果 (TRANSLATED 时 template 必须是已注册模板)"""
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if fields.get("status") == "TRANSLATED" and fields.get("template") not in TEMPLATES:
        raise HTTPException(status_code=400,
                            detail=f"TRANSLATED 必须指定已注册的模板名, 可选: {list(TEMPLATES)}")
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    row = await request.app.state.pool.fetchrow(
        f"UPDATE mq5_imports SET {sets} WHERE id = $1"
        " RETURNING id, name, status, assessment, template", import_id, *fields.values())
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return dict(row)


class Mq5Verify(BaseModel):
    params: dict            # 朋友调好的参数 (翻译后的键名)
    symbol: str
    timeframe: str = "M15"
    from_time: datetime
    to_time: datetime
    deals_text: str         # MT5 Strategy Tester 报告 Deals 表粘贴文本


@router.post("/strategies/mq5/{import_id}/verify")
async def mq5_verify(import_id: int, req: Mq5Verify, request: Request):
    """一致性验证: 原版EA成交记录 vs 翻译模板本地回测 → 一致率% (写回 mq5_imports)"""
    pool = request.app.state.pool
    imp = await pool.fetchrow("SELECT template FROM mq5_imports WHERE id=$1", import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="not found")
    if not imp["template"] or imp["template"] not in TEMPLATES:
        raise HTTPException(status_code=400, detail="该导入还没有对应的已注册模板 (需先 TRANSLATED)")
    if req.timeframe not in TF_SECONDS:
        raise HTTPException(status_code=400, detail="invalid timeframe")

    original = verify.parse_tester_deals(req.deals_text)
    if not original:
        raise HTTPException(status_code=400,
                            detail="未解析到入场记录 — 请从 Strategy Tester 的 Deals 表全选复制粘贴")

    point = await pool.fetchval("SELECT point FROM symbols WHERE symbol=$1", req.symbol)
    if point is None:
        raise HTTPException(status_code=400, detail=f"symbol {req.symbol} not in symbols table")
    m1 = await backtest.load_m1(pool, req.symbol, req.from_time, req.to_time)
    if m1 is None:
        raise HTTPException(status_code=400, detail=f"no M1 data for {req.symbol}, run /syncdata first")

    result = await asyncio.to_thread(
        backtest.run_backtest, m1, imp["template"], req.params, point, req.timeframe)
    ours = [(t["entry_time"], t["dir"]) for t in result["trades"]]
    cmp = verify.compare_entries(ours, original, TF_SECONDS[req.timeframe])

    await pool.execute(
        "UPDATE mq5_imports SET consistency=$2, verify_detail=$3, verified_at=now() WHERE id=$1",
        import_id, cmp["consistency"], cmp)
    logger.info("mq5 verify #%d: %.1f%% (ours=%d orig=%d)", import_id,
                cmp["consistency"], cmp["ours"], cmp["original"])
    return cmp


class StatusRequest(BaseModel):
    status: str  # CANDIDATE | DEMO | LIVE | ARCHIVED


@router.post("/strategies/{strategy_id}/status")
async def set_status(strategy_id: int, req: StatusRequest, request: Request):
    """准入漏斗状态流转(任意状态可互转, LIVE 也能撤回); 进入 DEMO/LIVE 时自动分配 magic_number。
    进入 DEMO/LIVE 前必须已有对应职能的执行主机, 否则策略转过去只会空等 worker。"""
    if req.status not in ("CANDIDATE", "DEMO", "LIVE", "ARCHIVED"):
        raise HTTPException(status_code=400, detail="invalid status")
    if req.status in ("DEMO", "LIVE"):
        role = req.status.lower()
        n = await request.app.state.pool.fetchval(
            "SELECT count(*) FROM mt5_hosts WHERE runner=$1 AND enabled", role)
        if not n:
            raise HTTPException(
                status_code=400,
                detail=f"没有已指派的 {role} 执行主机 — 先到 {req.status.capitalize()} 页指派主机, 再把策略转入 {req.status}")
    row = await request.app.state.pool.fetchrow(
        "UPDATE strategies SET status=$2::text,"
        " magic_number = CASE WHEN $2::text IN ('DEMO','LIVE')"
        "   THEN COALESCE(magic_number, 100000 + id) ELSE magic_number END"
        " WHERE id=$1 RETURNING id, name, status, magic_number",
        strategy_id, req.status)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    return dict(row)


# 孤儿策略: 品种已从主档删除、永远跑不了的策略(如旧 BTCUSD)。只算未归档的(归档=已处理)。
_ORPHAN_WHERE = "symbol NOT IN (SELECT symbol FROM symbols) AND status <> 'ARCHIVED'"


@router.get("/strategies/orphans")
async def orphans(request: Request):
    """列出孤儿策略(品种已删、未归档) — 供页面亮清单, 清理前先看清楚要清什么"""
    rows = await request.app.state.pool.fetch(
        f"SELECT id, name, symbol, status FROM strategies WHERE {_ORPHAN_WHERE}"
        " ORDER BY symbol, id")
    return {"orphans": [dict(r) for r in rows]}


@router.post("/strategies/orphans/archive")
async def archive_orphans(request: Request):
    """把孤儿策略批量归档(ARCHIVED, 可逆); 不删除, 留尸体避免重复生成"""
    rows = await request.app.state.pool.fetch(
        f"UPDATE strategies SET status='ARCHIVED' WHERE {_ORPHAN_WHERE} RETURNING id")
    return {"archived": len(rows)}
