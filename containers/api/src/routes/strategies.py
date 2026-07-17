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
        "   THEN COALESCE(magic_number, 100000 + id) ELSE magic_number END,"
        # 手动转入 ARCHIVED = 死因 manual; 转出归档则清死因(复活)
        " archive_reason = CASE WHEN $2::text = 'ARCHIVED' THEN 'manual' ELSE NULL END"
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
        f"UPDATE strategies SET status='ARCHIVED', archive_reason='orphan_symbol'"
        f" WHERE {_ORPHAN_WHERE} RETURNING id")
    return {"archived": len(rows)}


@router.get("/strategies/{strategy_id}/report")
async def ai_report(strategy_id: int, request: Request):
    """AI 成绩单(结构化 JSON, 纯数字无评语 — 事实只存一份, 表述现算):
    身份/参数 + 主品种回测(含 oos/by_year/mae/mfe) + 跨品种 + 可信度(对账) + 实盘
    + 同模板尸体(负样本: 参数+死因码)。喂给 AI 生成器做调参迭代的输入。"""
    pool = request.app.state.pool
    s = await pool.fetchrow(
        "SELECT id, name, template, params, symbol, timeframe, status, magic_number,"
        "       archive_reason FROM strategies WHERE id=$1", strategy_id)
    if s is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    bts = await pool.fetch(
        "SELECT symbol, broker, from_time, to_time, metrics, trades FROM backtests"
        " WHERE strategy_id=$1 ORDER BY (symbol = $2) DESC, symbol", strategy_id, s["symbol"])
    actual = await pool.fetchrow(
        "SELECT sum(trades) AS t, sum(wins) AS w, sum(profit) AS p"
        " FROM strategy_stats WHERE strategy_id=$1", strategy_id)
    dead = await pool.fetch(  # 同模板负样本: 已淘汰的参数 + 死因(AI 别再生成同类)
        "SELECT params, archive_reason FROM strategies"
        " WHERE template=$1 AND status='ARCHIVED' AND id<>$2"
        " ORDER BY updated_at DESC LIMIT 20", s["template"], strategy_id)
    # 单策略深分析是低频、单条的闭环动作 → 详细度优先, 不截断不省略(2026-07-16 定):
    # 归因聚合 + 每品种全量逐笔 + 实盘全量逐笔 + 对账全量(含逐笔对照/缺口归因/精度偏差)
    from src.routes.backtests import _analyze_trades, actual_attribution, compute_reconcile
    main_trades = next((b["trades"] for b in bts if b["symbol"] == s["symbol"]), None) or []
    attr_bt = _analyze_trades(main_trades, {}, [])
    attr_bt.pop("overfit", None)   # oos/跨品种已在 backtests 各行 metrics 里, 不重复
    attr_actual = await actual_attribution(pool, strategy_id)
    act_rows = await pool.fetch(  # 实盘逐笔全字段(单实例才几十笔, 全部信息不省略)
        "SELECT entry_time, exit_time, direction, volume, entry_price, exit_price, sl, tp,"
        "       profit, commission, swap, net_points, close_reason, env, broker, account"
        " FROM trades WHERE strategy_id=$1 ORDER BY entry_time", strategy_id)
    runtime = await pool.fetch(  # 运行区间原始数据(何时真实在跑; 对账窗口由它推导)
        "SELECT run_from, run_to, host FROM strategy_runtime WHERE strategy_id=$1"
        " ORDER BY run_from", strategy_id)
    envs = await pool.fetch(     # 实盘按环境拆分(demo/live 各自战绩快照)
        "SELECT env, trades, wins, profit, updated_at FROM strategy_stats"
        " WHERE strategy_id=$1 ORDER BY env", strategy_id)
    recon = await compute_reconcile(pool, strategy_id)  # 现算最新对账(与分析页同口径)

    def _cols(ts):  # 回测逐笔 → 紧凑列式(全字段: 出入场时间/价格/净点/原因/MAE/MFE)
        return {"cols": ["entry_time", "exit_time", "dir", "entry", "exit",
                         "points", "reason", "mae", "mfe"],
                "rows": [[t["entry_time"], t.get("exit_time"), t.get("dir"),
                          t.get("entry"), t.get("exit"), t.get("points"), t.get("reason"),
                          t.get("mae"), t.get("mfe")]
                         for t in sorted(ts, key=lambda x: x["entry_time"])]}
    return {
        "strategy": dict(s),
        # 主品种带全量逐笔; 交叉品种只带成绩汇总(角色是及格线筛查, 交叉不灵就不会深分析,
        # 逐笔属于过度供给 — 2026-07-16 定)
        "backtests": [{"symbol": b["symbol"], "broker": b["broker"],
                       "from": b["from_time"], "to": b["to_time"],
                       "is_main": b["symbol"] == s["symbol"], "metrics": b["metrics"],
                       **({"trades": _cols(b["trades"] or [])}
                          if b["symbol"] == s["symbol"] else {})}
                      for b in bts],
        "attribution_backtest": attr_bt if attr_bt.get("has_data") else None,   # 主品种回测归因
        "attribution_actual": attr_actual if attr_actual.get("has_data") else None,  # 实盘归因
        "trades_actual": {   # 实盘全量逐笔·全字段(含SL/TP/手数/佣金/库存费 — 调SL/TP的实证)
            "cols": ["entry_time", "exit_time", "dir", "volume", "entry_price", "exit_price",
                     "sl", "tp", "net_points", "profit", "commission", "swap",
                     "reason", "env", "broker", "account"],
            "rows": [[int(r["entry_time"].timestamp()),
                      (int(r["exit_time"].timestamp()) if r["exit_time"] else None),
                      r["direction"], float(r["volume"]) if r["volume"] is not None else None,
                      float(r["entry_price"]) if r["entry_price"] is not None else None,
                      float(r["exit_price"]) if r["exit_price"] is not None else None,
                      float(r["sl"]) if r["sl"] is not None else None,
                      float(r["tp"]) if r["tp"] is not None else None,
                      float(r["net_points"]) if r["net_points"] is not None else None,
                      float(r["profit"]),
                      float(r["commission"]) if r["commission"] is not None else None,
                      float(r["swap"]) if r["swap"] is not None else None,
                      r["close_reason"], r["env"], r["broker"], r["account"]]
                     for r in act_rows],
        },
        # 对账全量(可信度+校准): 匹配率/精度偏差/模式/对比窗口/逐笔对照(含缺口归因)
        "reconciliation": recon,
        "runtime": [{"from": r["run_from"], "to": r["run_to"], "host": r["host"]}
                    for r in runtime],   # 运行区间原始段(何时真实在跑)
        "actual": ({"trades": actual["t"], "wins": actual["w"], "profit": float(actual["p"]),
                    "by_env": {r["env"]: {"trades": r["trades"], "wins": r["wins"],
                                          "profit": float(r["profit"]),
                                          "updated_at": r["updated_at"]} for r in envs}}
                   if actual and actual["t"] else None),
        "failed_neighbors": [{"params": d["params"], "died_of": d["archive_reason"]}
                             for d in dead],
    }


# 淘汰死因码(schema/022): AI 负样本("这类参数死于什么"), 页面按码翻中文, 不收自由文本
ARCHIVE_REASONS = {"manual", "holdout_loss", "min_trades", "low_pf", "recon_fail",
                   "orphan_symbol", "other"}


class ArchiveRequest(BaseModel):
    strategy_ids: list[int]
    reason: str = "manual"  # 死因码, 见 ARCHIVE_REASONS


@router.post("/strategies/archive")
async def archive_batch(req: ArchiveRequest, request: Request):
    """按【明确列出的 ID】批量淘汰归档(标 ARCHIVED + 死因码, 可逆); 不删除 — 留尸体避免重复生成/回测。
    只处理请求里点名的 id, 不跟随任何查询过滤(防误伤全库)。
    LIVE(真钱在跑)不动, 需单独手动改, 防误杀; 已淘汰归档的跳过(幂等)。"""
    if not req.strategy_ids:
        raise HTTPException(status_code=400, detail="no strategy_ids")
    if req.reason not in ARCHIVE_REASONS:
        raise HTTPException(status_code=400,
                            detail=f"invalid reason, allowed: {sorted(ARCHIVE_REASONS)}")
    rows = await request.app.state.pool.fetch(
        "UPDATE strategies SET status='ARCHIVED', archive_reason=$2, updated_at=now()"
        " WHERE id = ANY($1) AND status NOT IN ('ARCHIVED', 'LIVE') RETURNING id",
        req.strategy_ids, req.reason)
    return {"archived": len(rows), "requested": len(req.strategy_ids)}
