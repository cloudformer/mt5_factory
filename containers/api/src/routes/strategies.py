"""/strategies — 策略实例的生成与生命周期 + MQ5 转化流水线

职责: 模板清单、批量生成(grid/random)、AI 调参收货(ai_candidates)、列表筛选、
     状态流转(准入漏斗)、MQ5 提交与跟踪(评估→翻译→纳入)。
策略逻辑本体在 strategy_core/ (回测与 Windows runner 共用同一份)。

扩展点:
- 新策略模板 = strategy_core/templates/ 加文件 + 注册 TEMPLATES (本文件不用改)
- 新参数来源 = 生产 combos 后走 services.instances 统一收货管道 (见 ai_candidates)
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from src.services import (backtest, identity, instances, prediction,
                          regime, usage, verify)
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
    mode: str = "random"  # grid=固定网格(有限) | random=随机采样(默认)
    count: int = 50       # random 模式下每个品种生成的数量
    label: str | None = None  # 批次标签(2026-07-27): 写进 basis(生因), 事后按批查找/分组统计用


@router.post("/strategies/generate")
async def generate(req: GenerateRequest, request: Request):
    """批量生成 CANDIDATE 实例 (重复组合自动跳过)"""
    if req.template not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"unknown template, available: {list(TEMPLATES)}")
    if req.timeframe not in TF_SECONDS:
        raise HTTPException(status_code=400, detail=f"invalid timeframe, available: {list(TF_SECONDS)}")
    if req.mode not in ("grid", "random"):
        raise HTTPException(status_code=400, detail="mode must be grid or random")

    pool = request.app.state.pool
    # 品种唯一数据源: 只能给已登记(经券商校验)的品种生成策略, 根治"给券商没有的品种生成"
    symbols = [s.strip().upper() for s in req.symbols if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    known = {r["symbol"] for r in await pool.fetch(
        "SELECT symbol FROM symbols WHERE symbol = ANY($1::text[])", symbols)}
    unknown = [s for s in symbols if s not in known]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"品种未登记: {', '.join(unknown)} — 先在下载页登记(会向券商校验)再生成策略")
    # 三种模式只负责"生产参数"; 校验/入库/去重/反馈统一走 services.instances 收货管道
    # (与 AI 调参页同一条路, 协议 = [{"params", "basis"}]) — 改规则只改管道一处
    created, total, truncated = 0, 0, 0
    rng = random.Random()
    for symbol in symbols:
        max_created = None
        basis = (req.label or "").strip() or req.mode   # 标签优先; 没填保持旧值 grid/random
        if req.mode == "grid":
            combos = [{"params": p, "basis": basis} for p in grid_combos(req.template)]
        else:  # random: 多采样抵消撞重(内存先去重), 管道里新建满 count 即停
            seen, combos = set(), []
            for _ in range(req.count * 5):
                p = random_combo(req.template, rng)
                if p is None:
                    break
                key = tuple(sorted(p.items()))
                if key in seen:
                    continue
                seen.add(key)
                combos.append({"params": p, "basis": basis})
            max_created = req.count
        r = await instances.create_instances(
            pool, req.template, symbol, req.timeframe, combos, max_created=max_created)
        created += len(r["created_ids"])
        total += len(r["results"])
        truncated += r["truncated"]
    logger.info("generated %d strategies (%s, mode=%s)", created, req.template, req.mode)
    return {"created": created, "skipped": total - created, "truncated": truncated,
            "batch_limit": r["batch_limit"], "mode": req.mode,
            "template": req.template, "symbols": req.symbols}


@router.get("/strategy_batches")
async def strategy_batches(request: Request, limit: int = 60, only_tested: int = 0,
                           min_n: int = 2):
    """批次清单(2026-08-12): 生成时填的标签 + 各自实例数/周期/回测进度 —
    回测页与分析页的批次下拉都吃它。

    only_tested(2026-08-13 补): 只列【有回测行】的批次。
    分析页(规律/筛选)本来就只能算有回测行的策略, 列出没法算的批次纯属挖坑 —
    Frank 就是选中一个 1 个策略且没回测的 AI 克隆"批次", 收到 400 才发现。
    回测页【不传】这个参数 —— 那边恰恰要的就是未测批次。

    注: basis 这一列身兼两职 —— 生成批次标签(grid/random 填的) + AI 克隆的生因
    (一整句话, 每克隆一个算一个"批次")。后者会把下拉刷屏, only_tested 顺带滤掉大半。
    """
    uid = identity.scope_uid(request)
    tested = ("count(*) FILTER (WHERE EXISTS (SELECT 1 FROM backtests b"
              "   WHERE b.strategy_id = s.id AND b.symbol = s.symbol))")
    rows = await request.app.state.pool.fetch(
        "SELECT basis, count(*)::int AS n,"
        "       string_agg(DISTINCT timeframe, ',' ORDER BY timeframe) AS timeframes,"
        f"       {tested}::int AS tested, max(created_at) AS last_at"
        "  FROM strategies s"
        " WHERE basis IS NOT NULL AND basis <> ''"
        + (" AND owner_id = $2" if uid else "") +
        " GROUP BY basis"
        + " HAVING count(*) >= " + str(max(int(min_n), 1))
        + (f" AND {tested} > 0" if only_tested else "") +
        " ORDER BY max(created_at) DESC LIMIT $1",
        limit, *([uid] if uid else []))
    return {"batches": [dict(r) for r in rows]}


@router.get("/strategies/names")
async def strategy_names(request: Request):
    """轻量名册: id/name/magic 全量(流水页 magic→策略名 归因用)。
    为什么单开: /strategies/status 每行带成绩包且有 limit — 库存超 limit 时
    归属列会一半真名一半"策略 #id"兜底(2026-07-26 实测 6000+ 库存踩中), 名册必须全量且轻。"""
    uid = identity.scope_uid(request)   # v5.6: 非 owner 只见自己的
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, magic_number FROM strategies"
        + (" WHERE owner_id = $1" if uid else "") + " ORDER BY id",
        *([uid] if uid else []))
    return {"strategies": [dict(r) for r in rows]}


@router.get("/strategies/status")
async def list_strategies(request: Request, status: Optional[str] = None,
                          symbol: Optional[str] = None, limit: int = 100,
                          host: Optional[str] = None):
    """策略实例列表, 按状态/品种筛选 (Windows runner 拉任务也走这里)。
    host=主机名(v5.0-B1 挂载认领): 该 worker 只拉指给自己的挂载(手数取挂载点值),
    与 status 组成两把钥匙防呆; 未注册/未传 host = 旧口径(角色全量), 老 runner 双向兼容。
    随附三方战绩 — web 页并排对比"回测质量 / demo / live 在券商是否一致":
      backtest: 最新一次回测指标 (backtests 表, 可能为 null)
      stats:    {"demo": {trades,wins,profit}, "live": {...}} (strategy_stats 表, 心跳快照;
                multi-account 安全: 先按 env 聚合再打包)"""
    cond, args = [], []
    vol_expr, join_mounts = "s.volume", ""
    if host:
        hr = await request.app.state.pool.fetchrow(
            "SELECT id FROM mt5_hosts WHERE name=$1 AND enabled", host)
        if hr:
            args.append(hr["id"])
            join_mounts = (f"  JOIN strategy_mounts m ON m.strategy_id = s.id"
                           f" AND m.enabled AND m.host_id = ${len(args)}")
            vol_expr = "COALESCE(m.volume, s.volume)"
    q = (f"SELECT s.id, s.name, s.template, s.symbol, s.timeframe, s.params, s.status,"
         f"       s.metadata, s.magic_number, {vol_expr} AS volume, sy.broker,"
         f"       b.metrics AS backtest, st.stats"
         "  FROM strategies s"
         f"{join_mounts}"
         "  LEFT JOIN symbols sy ON sy.symbol = s.symbol"  # 券商(来自品种主档)
         # 只取主品种回测 (symbol=s.symbol): 跨品种验证会写多品种行, 不能串到别品种成绩
         "  LEFT JOIN LATERAL (SELECT metrics FROM backtests"
         "                      WHERE strategy_id = s.id AND symbol = s.symbol"
         "                      ORDER BY id DESC LIMIT 1) b ON true"
         "  LEFT JOIN LATERAL (SELECT jsonb_object_agg(env, v) AS stats FROM ("
         "                       SELECT lower(env) AS env, jsonb_build_object("
         "                         'trades', sum(trades)::int, 'wins', sum(wins)::int,"
         "                         'profit', round(sum(profit)::numeric, 2)) AS v"
         "                       FROM strategy_stats WHERE strategy_id = s.id"
         "                       GROUP BY lower(env)) e) st ON true")
    if status:
        args.append(status); cond.append(f"s.status = ${len(args)}")
    if symbol:
        args.append(symbol); cond.append(f"s.symbol = ${len(args)}")
    uid = identity.scope_uid(request)   # v5.6 通电: 非 owner 只见自己的策略
    if uid:                             # (无身份的 runner/脚本不过滤, 行为与通电前一致)
        args.append(uid); cond.append(f"s.owner_id = ${len(args)}")
    if cond:
        q += " WHERE " + " AND ".join(cond)
    args.append(limit)
    q += f" ORDER BY s.id LIMIT ${len(args)}"
    rows = await request.app.state.pool.fetch(q, *args)
    out_rows = [dict(r) for r in rows]
    # regime 门(v0.3): 带门策略随行下发当日格(版本钉死在 metadata 里) — runner 据此裁决入场。
    # 只对带门策略做(常态列表零额外查询); 顺手自愈该版本时间线(新鲜时只是一次轻查询,
    # 每日首拉触发重算 → "当日格"的每日计算就靠这里, 无定时任务)
    pool = request.app.state.pool
    for r in out_rows:
        g = (r["metadata"] or {}).get("regime") if isinstance(r["metadata"], dict) else None
        if not (isinstance(g, dict) and g.get("cells")):
            continue
        vid = int(g["version"])
        try:
            await regime.ensure_timeline(pool, r["symbol"], vid)
        except Exception as e:
            logger.warning("status gate ensure v%d %s failed: %s", vid, r["symbol"], e)
        tl_last = await pool.fetchrow(
            "SELECT date, regime FROM regime_timeline"
            " WHERE version_id=$1 AND symbol=$2 ORDER BY date DESC LIMIT 1",
            vid, r["symbol"])
        r["regime_cell"] = tl_last["regime"] if tl_last else None
        r["regime_cell_date"] = tl_last["date"].isoformat() if tl_last else None
    # 默认手数(config 唯一源): runner 对 volume 为空的策略用它; web 下拉显示「X(默认)」
    vol_default = await pool.fetchval(
        "SELECT value FROM config WHERE key='volume_default'")
    return {"count": len(out_rows), "strategies": out_rows,
            "volume_default": vol_default}


@router.get("/strategies/tree")
async def strategy_tree(request: Request, template: Optional[str] = None,
                        symbol: Optional[str] = None,
                        timeframe: Optional[str] = None,
                        strategy_id: Optional[int] = None):
    """策略谱系(2026-08-02 与 Frank 定样版): 两级树 —
    参数实例平铺(AI 出身是 basis 描述, 不画层级), 门变体(同参数+metadata)挂父实例下
    (门离开父没有意义, 这才是真从属)。归档一律排除只回计数。
    成绩 = 主品种最新回测三值, 读时现拼零落库; 实例按净点降序未回测沉底。
    两个入口: 模板+品种(浏览) / strategy_id(直查该策略的家族 — 输门id自动定位到父)。"""
    pool = request.app.state.pool
    cond, args = [], []
    if strategy_id:
        await identity.assert_strategy_visible(pool, request, strategy_id)
        s = await pool.fetchrow(
            "SELECT id, template, symbol, status, parent_id, metadata"
            " FROM strategies WHERE id=$1", strategy_id)
        if s is None:
            raise HTTPException(status_code=404, detail=f"策略 #{strategy_id} 不存在")
        if s["status"] == "ARCHIVED":
            raise HTTPException(status_code=400, detail=f"#{strategy_id} 已归档 — 谱系不显示归档")
        g = (s["metadata"] or {}).get("regime") if isinstance(s["metadata"], dict) else None
        root_id = (s["parent_id"] if (isinstance(g, dict) and g.get("cells")
                                      and s["parent_id"]) else s["id"])
        template, symbol = s["template"], s["symbol"]
        args.extend([template, symbol, root_id])
        # 直查=只看上下级: 父实例本身 + 它的门变体(metadata 带 regime 的子代)。
        # AI 调参子代 parent_id 也指向父, 但那不是层级(Frank 定) — 直查不捞它们
        cond = ["s.template = $1", "s.symbol = $2",
                "(s.id = $3 OR (s.parent_id = $3 AND s.metadata ? 'regime'))"]
    elif template and symbol:
        cond, args = ["s.template = $1", "s.symbol = $2"], [template, symbol]
        if timeframe:
            args.append(timeframe)
            cond.append(f"s.timeframe = ${len(args)}")
    else:
        raise HTTPException(status_code=400, detail="需要 模板+品种, 或 策略ID")
    uid = identity.scope_uid(request)
    if uid:
        args.append(uid)
        cond.append(f"s.owner_id = ${len(args)}")
    where = " AND ".join(cond)
    rows = await pool.fetch(
        f"SELECT s.id, s.name, s.params, s.metadata, s.status, s.parent_id, s.basis,"
        f"       s.timeframe, b.metrics, b.from_time, b.to_time"
        f"  FROM strategies s"
        f"  LEFT JOIN LATERAL (SELECT metrics, from_time, to_time FROM backtests"
        f"        WHERE strategy_id = s.id AND symbol = s.symbol"
        f"        ORDER BY id DESC LIMIT 1) b ON true"
        f" WHERE {where} AND s.status <> 'ARCHIVED'"
        f" ORDER BY s.id", *args)
    archived = await pool.fetchval(
        f"SELECT count(*) FROM strategies s WHERE {where} AND s.status = 'ARCHIVED'", *args)

    def _node(r):
        mt = r["metrics"] or {}
        g = (r["metadata"] or {}).get("regime") if isinstance(r["metadata"], dict) else None
        return {"id": r["id"], "name": r["name"], "params": r["params"],
                "status": r["status"], "basis": r["basis"], "timeframe": r["timeframe"],
                "parent_id": r["parent_id"],
                # metadata 原文透传(展开详情用): regime 只是里面一个键, 将来加 trail 等
                # 新键这里自动跟着显示 — 不从 gate 反拼, 防漏显
                "metadata": r["metadata"] or {},
                "gate": g if (isinstance(g, dict) and g.get("cells")) else None,
                "trades": mt.get("trades"), "win_rate": mt.get("win_rate"),
                "net_points": mt.get("net_points"),
                "pf": mt.get("profit_factor"),
                # 回测窗口(2026-08-02 Frank 定): 各实例窗口不一(20年/5年/批量半年),
                # 不标出来数字会被误互比(对比三铁律)
                "bt_from": r["from_time"].isoformat() if r["from_time"] else None,
                "bt_to": r["to_time"].isoformat() if r["to_time"] else None,
                "gates": []}

    by_id, instances, gate_nodes = {}, [], []
    for r in rows:
        n = _node(r)
        (gate_nodes if n["gate"] else instances).append(n)
        by_id[n["id"]] = n
    for g in gate_nodes:
        p = by_id.get(g["parent_id"])
        if p is not None and p["gate"] is None:
            p["gates"].append(g)
        else:
            g["orphan"] = True   # 父不可见(归档/越筛选) → 顶层平铺如实标注, 不隐藏
            instances.append(g)

    def _key(n):
        return (n["net_points"] is None, -(n["net_points"] or 0))
    instances.sort(key=_key)
    for p in instances:
        p["gates"].sort(key=_key)
    return {"template": template, "symbol": symbol, "timeframe": timeframe,
            "instances": instances, "archived": archived, "count": len(rows)}


class CloneGateRequest(BaseModel):
    version: int   # regime 版本 id, 必须钉死(null/default 拒收 — 校验在 gate_error)
    cells: dict    # {格: 倍率 0.5~1}, 未列格=不开新仓
    # 门的来源备注(2026-08-10 Frank 要, 与 AI 调参尾标同款): AI 自报模型名+信心+排名
    # → 追加进 basis, 事后一眼知道这个门谁给的建议; 人手勾的不传 = 行为不变
    note: Optional[str] = None


@router.post("/strategies/{strategy_id}/clone_gate")
async def clone_gate(strategy_id: int, req: CloneGateRequest, request: Request):
    """克隆带门(v0.3 开工清单#3): 父参数原样 + metadata.regime 门 = 新实例
    (parent_id 谱系, 独立成绩独立魔数, 走统一收货管道)。
    父永远是干净的全量基准; 同门重复克隆撞唯一约束 → 返回现有 id(管道语义, 不重不漏)。"""
    pool = request.app.state.pool
    await identity.assert_strategy_visible(pool, request, strategy_id)
    parent = await pool.fetchrow(
        "SELECT id, template, symbol, timeframe, params, parent_id, metadata"
        " FROM strategies WHERE id=$1", strategy_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    # 谱系扁平化(2026-08-08 Frank 定: 一套参数=一族, 门变体都是平辈兄弟不叠罗汉):
    # 从带门实例页面点克隆 → 新实例挂到它的无门根上(顺链上溯), 不挂带门实例下。
    # 红利: 预测验证的增益基线(parent 的回测行)永远是无门全量, 语义不歪
    root_id = strategy_id
    seen = {root_id}
    row = parent
    while isinstance(row["metadata"], dict) and (row["metadata"] or {}).get("regime")             and row["parent_id"] and row["parent_id"] not in seen:
        seen.add(row["parent_id"])
        nxt = await pool.fetchrow(
            "SELECT id, parent_id, metadata FROM strategies WHERE id=$1", row["parent_id"])
        if nxt is None:
            break
        root_id = nxt["id"]
        row = nxt
    gate = {"version": req.version, "cells": req.cells}
    err = await instances.gate_error(pool, gate)
    if err:
        raise HTTPException(status_code=400, detail=err)
    suffix = f"-gate-v{req.version}-" + "-".join(
        f"{k}{float(req.cells[k]):g}" for k in sorted(req.cells))
    r = await instances.create_instances(
        pool, parent["template"], parent["symbol"], parent["timeframe"],
        [{"params": parent["params"],
          "basis": f"克隆带门 parent=#{root_id}{suffix}"
                   + (f" 〔{req.note.strip()[:120]}〕" if (req.note or "").strip() else "")}],
        parent_id=root_id, metadata={"regime": gate}, name_suffix=suffix,
        trust_params=True)   # 父参数来自库内现有行, 参数空间演化不应挡克隆
    out = r["results"][0]
    out["created"] = "id" in out
    return out


@router.get("/prediction/board")
async def prediction_board(request: Request, batch: int = 30, scope: str = "gated"):
    """策略预测看板(2026-08-10 Frank 定): 锚=创建时间 — 过去=整个回测窗按每 batch 笔
    一批的 PF 序列, 之后=创建日起合并一个 PF。batch 是页面控件传参(不落库, 钳 5~500);
    scope=gated(默认, regime 带门) / all(有回测行的全部)。读时现拼零落库。"""
    pool = request.app.state.pool
    return {"rows": await prediction.board(pool, batch=max(5, min(500, batch)),
                                           gated_only=(scope != "all"))}


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
    # 挂载联动(v5.0-B1, 2026-07-24 与 Frank 定): 分账户世界不"全挂"——
    # DEMO/LIVE → 挂到 owner 池里该角色"挂载数最少"的一台(单台=它, 新策略自动流向空机;
    # 该角色已有挂载则不动 — 保住既有落位, 换挂/多挂是 B2 的 UI 活);
    # CANDIDATE/ARCHIVED → 清空挂载即停跑。runner 按挂载认领见 list_strategies(host=)。
    if req.status in ("DEMO", "LIVE"):
        await request.app.state.pool.execute(
            "INSERT INTO strategy_mounts (strategy_id, host_id, volume)"
            " SELECT s.id, h.id, s.volume FROM strategies s, mt5_hosts h"
            " WHERE s.id=$1 AND h.runner=$2 AND h.enabled AND h.owner_id = s.owner_id"
            "   AND NOT EXISTS (SELECT 1 FROM strategy_mounts m0"
            "                     JOIN mt5_hosts h0 ON h0.id = m0.host_id"
            "                    WHERE m0.strategy_id = $1 AND h0.runner = $2)"
            " ORDER BY (SELECT count(*) FROM strategy_mounts m2 WHERE m2.host_id = h.id), h.id"
            " LIMIT 1"
            " ON CONFLICT DO NOTHING", strategy_id, req.status.lower())
    else:
        await request.app.state.pool.execute(
            "DELETE FROM strategy_mounts WHERE strategy_id=$1", strategy_id)
    return dict(row)



async def _trail_window(pool, strategy_id: int, symbol: str):
    """移动止损对比/调优批跑的数据窗口(2026-07-29 时间窗保护): 优先用该策略主品种
    成绩单(backtests)存的 from/to — 对比三铁律: 和被比较的排名成绩同窗才可比;
    没跑过回测则回落 config 批量默认窗口(从现在往回数)。"""
    row = await pool.fetchrow(
        "SELECT from_time, to_time FROM backtests WHERE strategy_id=$1 AND symbol=$2",
        strategy_id, symbol)
    if row:
        return row["from_time"], row["to_time"]
    win = int(await pool.fetchval(
        "SELECT value FROM config WHERE key='backtest_window_days'") or 180)
    now = datetime.now(timezone.utc)
    return now - timedelta(days=win), now


@router.get("/strategies/{strategy_id}/trail_compare")
async def trail_compare(strategy_id: int, request: Request, variant: Optional[str] = None,
                        gap: Optional[int] = None, start: Optional[int] = None,
                        k: Optional[float] = None):
    """移动止损四档对比(v0.9 第3步): 同一策略全量回测跑 关/固定/保本/ATR 四版,
    内存现算不落库(排名/成绩仍用 backtests 表, 两口径各答各的)。
    档位参数: 策略 params.trail 里有该类就用它, 否则用数据自适应探针
    (fixed.gap=平均M1波幅×2 点, breakeven 同 gap+start=gap×2, atr k=2/period=14) — 免先填参数。"""
    pool = request.app.state.pool
    await identity.assert_strategy_visible(pool, request, strategy_id)
    s = await pool.fetchrow(
        "SELECT s.template, s.params, s.symbol, s.timeframe, sym.point FROM strategies s"
        " LEFT JOIN symbols sym ON sym.symbol = s.symbol WHERE s.id=$1", strategy_id)
    if s is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    if not s["point"] or s["timeframe"] not in backtest.TF_SECONDS:
        raise HTTPException(status_code=400, detail="品种未登记或周期不支持")
    w_from, w_to = await _trail_window(pool, strategy_id, s["symbol"])
    m1 = await backtest.load_m1(pool, s["symbol"], w_from, w_to)
    if m1 is None:
        raise HTTPException(status_code=400, detail=f"{s['symbol']} 无 M1 数据, 先去下载")
    cfg = await pool.fetchval("SELECT value FROM config WHERE key='backtest_costs'") or {}
    costs = {"slippage_points": cfg.get("slippage_points", backtest.DEFAULT_SLIPPAGE_POINTS),
             "commission_points": cfg.get("commission_points", backtest.DEFAULT_COMMISSION_POINTS),
             "spread_points": cfg.get("spread_points")}
    point = float(s["point"])
    import numpy as np
    probe_gap = max(int(round(float(np.mean(m1["high"] - m1["low"])) / point * 2)), 10)
    own = (s["params"] or {}).get("trail") or {}

    # 参数优先级: 页面手填(gap/start/k, 调试试数值) > 策略自己的 params.trail > 自适应探针
    g = gap or probe_gap
    st = start or g * 2
    kk = k or 2.0

    def _variant(t):
        if t == "off":
            return None
        v = {"active": t}
        if t == "fixed":
            v["fixed"] = {"gap": g} if (gap or not own.get(t)) else own[t]
        elif t == "breakeven":
            v["breakeven"] = ({"gap": g, "start": st}
                              if (gap or start or not own.get(t)) else own[t])
        else:  # atr 组: k 必填; start 可选(手填 start 时也作用于 atr 档 — 启动阈值对三类通用)
            v["atr"] = ({"k": kk, "period": 14, **({"start": start} if start else {})}
                        if (k or start or not own.get(t)) else own[t])
        if "keep_tp" in own:
            v["keep_tp"] = own["keep_tp"]
        return v

    # variant=某档: 只算那一档并附全量逐笔(点「明细」用) — 同样内存现算不落库
    types = (variant,) if variant in ("off", "fixed", "breakeven", "atr") \
        else ("off", "fixed", "breakeven", "atr")
    tl = {}
    if variant:  # 明细模式附每笔入场日格子(v2.5, 现拼不落库); 时间线缺失不挡明细
        try:
            await regime.ensure_timeline(pool, s["symbol"])
        except Exception as e:
            logger.warning("regime ensure %s failed: %s", s["symbol"], e)
        tl = await regime.tl_map(pool, s["symbol"])   # 当前默认版本(v0.2 版本化)
    rows = []
    for t in types:
        p = dict(s["params"] or {})
        p.pop("trail", None)
        tc = _variant(t)
        if tc:
            p["trail"] = tc
        res = await asyncio.to_thread(
            backtest.run_backtest, m1, s["template"], p, point, s["timeframe"],
            oos_split=None, **costs)
        mtr = res["metrics"]
        row = {"type": t, "cfg": tc, "trades": mtr.get("trades"),
               "net_points": mtr.get("net_points"), "win_rate": mtr.get("win_rate"),
               "profit_factor": mtr.get("profit_factor"),
               "max_dd_points": mtr.get("max_dd_points"),
               "tsl": sum(1 for x in res["trades"]
                          if str(x.get("reason", "")).startswith("tsl"))}
        if variant:  # 明细模式: 附逐笔(紧凑列式, 与成绩单同构) + 入场日 regime 格子
            row["detail"] = {"cols": ["entry_time", "exit_time", "dir", "entry", "exit",
                                      "points", "reason", "regime"],
                             "rows": [[x["entry_time"], x.get("exit_time"), x.get("dir"),
                                       x.get("entry"), x.get("exit"), x.get("points"),
                                       x.get("reason"),
                                       tl.get(datetime.fromtimestamp(
                                           x["entry_time"], tz=timezone.utc).date())]
                                      for x in res["trades"]]}
        rows.append(row)
    return {"strategy_id": strategy_id, "current": own.get("active"),
            "probe_gap": probe_gap, "variants": rows}


class TrailRequest(BaseModel):
    trail: Optional[dict] = None   # null = 清除(回落全局默认 trail_default)


@router.post("/strategies/{strategy_id}/trail")
async def set_trail(strategy_id: int, req: TrailRequest, request: Request):
    """把某档移动止损写进策略 params.trail(对比页「用这档」); null=清除回落全局默认。
    生效范围: 全量回测(重跑后)/对账重放/实盘 runner(第4步接通后) — 同一份配置三处一致。"""
    if req.trail is not None:
        t = req.trail.get("active")
        if t not in ("fixed", "breakeven", "atr"):
            raise HTTPException(status_code=400, detail="trail.active 须为 fixed/breakeven/atr")
        if not isinstance(req.trail.get(t), dict):
            raise HTTPException(status_code=400, detail=f"缺 {t} 的参数组")
    if req.trail is None:
        row = await request.app.state.pool.fetchrow(
            "UPDATE strategies SET params = params - 'trail', updated_at=now()"
            " WHERE id=$1 RETURNING id", strategy_id)
    else:
        row = await request.app.state.pool.fetchrow(
            "UPDATE strategies SET params = jsonb_set(params, '{trail}', $2::jsonb),"
            " updated_at=now() WHERE id=$1 RETURNING id", strategy_id, req.trail)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    logger.info("strategy #%d trail -> %s", strategy_id, req.trail)
    return {"id": strategy_id, "trail": req.trail}


class BasisRequest(BaseModel):
    basis: str


@router.post("/strategies/{strategy_id}/basis")
async def set_basis(strategy_id: int, req: BasisRequest, request: Request):
    """编辑备注(basis): 生成时是 AI 生因, 之后人工可就地改/补(当前版本唯一可编辑的注释)。"""
    val = req.basis.strip() or None
    row = await request.app.state.pool.fetchrow(
        "UPDATE strategies SET basis=$2, updated_at=now() WHERE id=$1 RETURNING id, basis",
        strategy_id, val)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    return dict(row)


class VolumeRequest(BaseModel):
    volume: Optional[float] = None  # None/空 = 清除, runner 回落到自己的 env 默认(0.01)


@router.post("/strategies/{strategy_id}/volume")
async def set_volume(strategy_id: int, req: VolumeRequest, request: Request):
    """设置每策略下单手数(仓位管理最小版): runner 每轮从 DB 拉配置, 下一单即生效(不用重启)。
    空 = 清除, 回落 worker env 默认。回测不受影响(净点与手数无关), 折算金额两边同一系数。"""
    if req.volume is not None and not (0 < req.volume <= 100):
        raise HTTPException(status_code=400, detail="volume 须在 (0, 100] 之间, 或留空=用默认")
    row = await request.app.state.pool.fetchrow(
        "UPDATE strategies SET volume=$2, updated_at=now()"
        " WHERE id=$1 RETURNING id, name, volume, status", strategy_id, req.volume)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    logger.info("strategy #%d volume -> %s", strategy_id, req.volume)
    return dict(row)


class VisibilityRequest(BaseModel):
    visibility: str


@router.post("/strategies/{strategy_id}/visibility")
async def set_visibility(strategy_id: int, req: VisibilityRequest, request: Request):
    """改可见性(v5.4): private=只有自己 / public=全可见可fork / shared=只给汇总可盲测订阅。
    现在只是打标(执法在 v5.6 输出层裁剪); 默认 private, 上市场永远是逐个主动打标。"""
    if req.visibility not in ("private", "public", "shared"):
        raise HTTPException(status_code=400, detail="visibility 须为 private/public/shared")
    row = await request.app.state.pool.fetchrow(
        "UPDATE strategies SET visibility=$2, updated_at=now()"
        " WHERE id=$1 RETURNING id, name, visibility", strategy_id, req.visibility)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    logger.info("strategy #%d visibility -> %s", strategy_id, req.visibility)
    return dict(row)


class MountRequest(BaseModel):
    host_id: int
    volume: Optional[float] = None   # 空=该挂载点回落 策略手数→全局默认


@router.get("/strategies/mounts")
async def list_mounts(request: Request, ids: str):
    """批量取挂载(策略列表页整页一次取; ids=逗号分隔的策略id)"""
    try:
        idl = [int(s) for s in ids.split(",") if s.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids 须为逗号分隔的数字")
    if not idl:
        return {"mounts": {}}
    rows = await request.app.state.pool.fetch(
        "SELECT m.strategy_id, m.host_id, h.name AS host, h.runner, m.volume, m.enabled"
        "  FROM strategy_mounts m JOIN mt5_hosts h ON h.id = m.host_id"
        " WHERE m.strategy_id = ANY($1) ORDER BY h.name", idl)
    out: dict = {}
    for r in rows:
        out.setdefault(str(r["strategy_id"]), []).append(dict(r))
    return {"mounts": out}


@router.post("/strategies/{strategy_id}/mounts")
async def set_mount(strategy_id: int, req: MountRequest, request: Request):
    """挂载/改挂载点手数(UPSERT enabled=true)。一策略可挂多台该角色 worker(多账户同时跑,
    trades 按 account 天然分开)。两把钥匙防呆: host 角色必须匹配策略状态; owner 匹配。"""
    if req.volume is not None and not (0 < req.volume <= 100):
        raise HTTPException(status_code=400, detail="volume 须在 (0, 100] 之间, 或留空=用默认")
    pool = request.app.state.pool
    s = await pool.fetchrow(
        "SELECT id, status, owner_id FROM strategies WHERE id=$1", strategy_id)
    if s is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    if s["status"] == "ARCHIVED":
        raise HTTPException(status_code=400, detail="已归档不可挂载 — 先切回空闲(候选)复活")
    h = await pool.fetchrow(
        "SELECT id, name, runner, enabled, owner_id FROM mt5_hosts WHERE id=$1", req.host_id)
    if h is None or not h["enabled"]:
        raise HTTPException(status_code=404, detail="host 不存在或未启用")
    if not h["runner"]:
        raise HTTPException(status_code=400,
                            detail=f"主机 {h['name']} 未指派运行角色(demo/live) — 先去 Workers 页指派")
    if h["owner_id"] != s["owner_id"]:
        raise HTTPException(status_code=400, detail="不能挂到别人的 worker")
    # 挂载=唯一意图(2026-08-02 Frank 定, 简单止血): 状态自动跟所挂机器的角色走 —
    # 不再要求先切状态再挂载(旧双钥匙摩擦)。挂 demo 机→模拟, 挂 live 机→真金;
    # magic 首次进入执行态时分配(与 set_status 同规则)。跨池改挂时旧池挂载行留库
    # (记忆落位, 状态钥匙让它失效, v7.4 统一归置)。
    new_status = h["runner"].upper()
    if s["status"] != new_status:
        await pool.execute(
            "UPDATE strategies SET status=$2,"
            " magic_number = COALESCE(magic_number, 100000 + id) WHERE id=$1",
            strategy_id, new_status)
        logger.info("mount auto-status #%d: %s -> %s (host %s)",
                    strategy_id, s["status"], new_status, h["name"])
    await pool.execute(
        "INSERT INTO strategy_mounts (strategy_id, host_id, volume) VALUES ($1, $2, $3)"
        " ON CONFLICT (strategy_id, host_id) DO UPDATE SET"
        "   volume = EXCLUDED.volume, enabled = true",
        strategy_id, req.host_id, req.volume)
    logger.info("mount #%d -> %s volume=%s", strategy_id, h["name"], req.volume)
    return {"strategy_id": strategy_id, "host": h["name"], "volume": req.volume,
            "status": new_status}


@router.delete("/strategies/{strategy_id}/mounts/{host_id}")
async def del_mount(strategy_id: int, host_id: int, request: Request):
    """卸载 = 软停用(enabled=false): 保留手数记忆; 状态联动的 NOT EXISTS 不会自动复活它
    (想复活: 挂载下拉重新挂, 或状态切走再切回=清空重挂)。"""
    pool = request.app.state.pool
    n = await pool.execute(
        "UPDATE strategy_mounts SET enabled=false WHERE strategy_id=$1 AND host_id=$2",
        strategy_id, host_id)
    if n == "UPDATE 0":
        raise HTTPException(status_code=404, detail="mount not found")
    remaining = await pool.fetchval(
        "SELECT count(*) FROM strategy_mounts WHERE strategy_id=$1 AND enabled", strategy_id)
    logger.info("unmount #%d host=%d remaining=%d", strategy_id, host_id, remaining)
    return {"strategy_id": strategy_id, "host_id": host_id, "remaining": int(remaining)}


# 孤儿策略: 品种已从主档删除、永远跑不了的策略(如旧 BTCUSD)。只算未归档的(归档=已处理)。
_ORPHAN_WHERE = "symbol NOT IN (SELECT symbol FROM symbols) AND status <> 'ARCHIVED'"


@router.get("/strategies/orphans")
async def orphans(request: Request):
    """列出孤儿策略(品种已删、未归档) — 供页面亮清单, 清理前先看清楚要清什么"""
    uid = identity.scope_uid(request)   # v5.6 通电: 非 owner 只见自己的
    rows = await request.app.state.pool.fetch(
        f"SELECT id, name, symbol, status FROM strategies WHERE {_ORPHAN_WHERE}"
        + (" AND owner_id = $1" if uid else "") + " ORDER BY symbol, id",
        *([uid] if uid else []))
    return {"orphans": [dict(r) for r in rows]}


@router.post("/strategies/orphans/archive")
async def archive_orphans(request: Request):
    """把孤儿策略批量归档(ARCHIVED, 可逆); 不删除, 留尸体避免重复生成"""
    rows = await request.app.state.pool.fetch(
        f"UPDATE strategies SET status='ARCHIVED', archive_reason='orphan_symbol'"
        f" WHERE {_ORPHAN_WHERE} RETURNING id")
    return {"archived": len(rows)}


@router.get("/strategies/{strategy_id}/profile")
async def strategy_profile(strategy_id: int, request: Request):
    """策略 Profile(v0.7 批次2): 结论级完整画像, 读时现拼零落库 —
    base/tags履历/stability(20·5·2年窗, 与oos_v2同刀法)/oos六段结论/states(格×战绩)/
    live战绩+对账/prediction(批次3)。原始档案(全量逐笔)在 /report(AI成绩单), 分工不重复。"""
    pool = request.app.state.pool
    await identity.assert_strategy_visible(pool, request, strategy_id)
    from src.services import profile as profile_svc
    prof = await profile_svc.build(pool, strategy_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    return prof


_REGIME_AI_PROMPT_HEAD = """\
# 任务(两问一次做完, 报告分三块按序写): ⓪验收自证 ①Regime 口径评价+建议版本 ②八象限配置
数据是全部 regime 版本的预聚合战绩。请**严格按 ⓪→①→② 的顺序**组织报告:
  ⓪ data_check: 是否用代码核算(computed_by) + 5 个探针答案 + (跑了置换则给 p 值);
  ① regime_report: 先讲清楚哪个版本更有规律、为什么, 给 ranking 与【建议版本】;
  ② strategy_report: 在建议版本下, 把**八个象限逐格**给出建议手数倍率与理由(含不选的格)。

## 数据形态(精简模式 — 系统已在数据库侧完成贴格与聚合, 你【不需要也不得】重算)
1. strategy: 模板/参数/品种/周期(主货币对全量悲观口径回测, 区间=backtest_window)
2. regime_versions[]: 每个口径版本给
   - params: 格子怎么算(长均线/短均线/ATR 等算法参数, 无未来函数)
   - coverage: 时间线对回测区间的覆盖("全量"=覆盖整个回测区间; 未全量的版本结论必须注明受限)
   - year_cells: 年×格战绩表, 行=[年, 格, 笔数, 赢笔数, 毛利点, 毛损点] — 第一分析对象
   - month_cells: 月×格战绩表, 行=[YYMM, 格, 笔数, 赢, 毛利, 毛损] — 辅助证据
   任意切片: PF=Σ毛利/Σ毛损, 净点=毛利-毛损, 胜率=Σ赢/Σ笔; 格="unlabeled"=当日无标签(不评格)。
   所有数字由系统数据库精确算出 — 你的工作是【读表和判断】; 引用的每个数字必须原样取自
   表格或由表格行直接加总, 禁止估算/杜撰。

## 第0步(必做, 任何情况必答): 完整性探针 — 防数据缺段/略读
下方 probe_keys 点名了若干 "v版本|年|格" — 在对应版本的 year_cells 里找到那一行,
在输出 data_check.probes 里原样照抄该行的 [笔数, 毛利点, 毛损点]。
这是查表不是计算, 不许为 null; 我方持有真实答案逐一核对, 答错 = 你没收到/没读全数据,
整份作废。确实没收到第①段数据 → probes 尽力回答, mismatch 写明缺什么, 两份报告置 null。

## 第一问: Regime 报告 — 评的是【口径】不是策略
用本策略全量回测当探针(区间见 backtest_window), 评价 N 个 regime 版本谁更有规律。判定算法:
- 对每个格: 从 year_cells 拉出该格【逐年】PF 序列, 看"格内跨年 PF 是否大致相同"(离散度小=稳定)。
- 例: 某版本下 AAA 格各年 PF 都≈0.7, BBB 格各年都≈1.9 → 这就是最好的 regime —
  格内跨时间稳定 + 格间拉得开。【稳定亏也是规律】(那格不选就是了, 见第二问)。
- 测量值用策略 PF, 但评的是口径的稳定性, 与策略赚不赚钱无关。
- 年切片为主; month_cells 保留为辅助证据(年都没规律月更不行; 格×月常<5笔, 样本薄只作参考)。
- 客观全量呈现: 全部格全部切片如实摆数字, 1 笔也正常显示, 不预设立场不隐藏;
  唯一标注 = 切片 <5 笔标"样本不足"。

## 三道必做检验(不做 = 结论不合格, 数字填进 versions[].metrics)
凭"看起来稳"下结论不算数, 每个版本必须给出以下三项并据此定 regularity:

1) **周期基准剥离(最容易犯的错)**: 先算【整个策略】不分格的分段基准 PF
   (至少 早段 vs 近5年 两个)。若两段基准差异明显 = 该策略存在全局周期漂移
   (整体变好/变坏), 此时"近5年各格 PF 普遍变好"多半是时代红利, 不是格的功劳。
   → 每个格必须同时给【绝对 PF】与【相对分 = 格PF ÷ 同期全局基准PF】,
     只有两者同向的格才算真有效; 只靠绝对分选格属于把周期红利记到格头上。

2) **格间分离 vs 格内离散(仅诊断, 不作加分项)**: separation = 各格全量 PF 的离散程度
   (取 log 后按笔数加权求标准差); dispersion = 每格逐年 PF 的离散程度(同法算完按笔数
   加权汇总); ratio = separation / dispersion。
   ⚠ 实证(2026-08-11 置换检验): ratio 的**随机水平本身就有 0.20~0.23**, 常见观测值
   全落在零分布内 → **ratio 高不构成任何版本的加分理由**(高值多半是笔数分布造成的假象)。
   只作诊断报出; 若 dispersion 是 separation 的数倍, confidence 最高 medium。

3) **可复现性检验(主判据)**: half_rho + rho_blocks。
   - half_rho: 按**累计笔数中位数**把交易对半(不许按年份中点 — 交易早年稀近年密,
     按年切两半笔数悬殊), 两半各算八格 PF 并排序, 报 Spearman 相关。
   - rho_blocks: 区间等分成 4 个等长块, 各算八格 PF, 报 6 对块间排序相关的均值。
   - 二者矛盾(一个明显为正另一个≈0/为负) = 单一切点的巧合 → 以 rho_blocks 为准。

## 显著性 = 置换零分布(2026-08-11 定, 强中弱的唯一方法论)
绝对阈值不可靠 —— 随机水平取决于笔数分布与切片结构, 必须先标准化:

**置换零分布(首选, 有代码工具就做)**: 在**每个时间切片内部随机打乱格标签**
(保持该切片的笔数/毛利/毛损完全不变, 只切断"格↔业绩"的对应), 重复 ≥1000 次,
得到 ratio / half_rho / rho_blocks 在"格标签完全无信息"时的分布 →
报每项的 z 与 p(单尾, 观测值在零分布中的位置), 并置 null_method="permutation"。

**分档规则(用 p 值, 不用绝对值)**:
- 强 = 三项中 ≥2 项 p<0.05, 且其中至少一项是可复现性检验(half_rho / rho_blocks);
- 中 = 恰好 1 项 p<0.05, 或 ≥2 项 p<0.10;
- 弱 = 无一项 p<0.05。
**多重比较(2026-08-11 修正: 别把族定得过大)**: 本方法【预先指定 rho_blocks 为主判据】
(ratio 已知不携带信息只作诊断; half_rho 是单切点, 稳健性不如块均值), 因此校正只对
【主判据 × 版本数】做: **α = 0.05 / N版本**(4 版本即 0.0125), 不要除以 3N。
- 主判据 p < α → 该版本【硬显著】, 可据此定第一名, bonferroni_pass 里写 "rho_blocks";
- 主判据 0.05 > p ≥ α → 边缘, 只能参考;
- 主判据 p ≥ 0.05 → 与随机无异。
half_rho / ratio 作为次要诊断报出即可, 不参与校正、也不用来定名次。
【统计够格 ≠ 可以放心用】若硬显著的版本另有实质问题(如 half_rho 明显为负 = 前后半段
排序反向, 或某些格样本过稀), 仍排第一但必须在 evidence 与 pick_note 里写明这些担忧,
并据此把 confidence 压到 medium。

**跑不了置换时的兜底**: 用经验阈值 ratio≥0.35 / half_rho≥0.45 / rho_blocks≥0.22
(约为常见数据规模下零分布 p95 的上包络), 并置 null_method="fixed_threshold"
+ 在 evidence 里注明"未做置换检验, 阈值为经验值, 显著性未经标准化"。

**措辞纪律**: 观测值落在零分布内时, 写"与随机标签无异"而非"弱于噪音" —— 前者是
"这项指标不携带信息", 后者暗示"有信息但被噪音盖住", 二者结论不同别混用。

## 版本排序不稳时的兜底(常见情形, 别硬排名次)
若无任何版本有 p<0.05 的项, 或各版本显著项相同/都不显著, 如实写明
"版本排序不稳定, 名次对切点/口径敏感"(不要给出貌似精确的全序), 改按【坏格识别的一致性】定第一名 ——
哪个版本能把跨版本公认的坏格分得最清楚(相对分最低、两段同向、样本最厚), 选哪个。
理由: 排除坏格是本方法最可复现的产出, 好格排名与版本名次都是弱信号。

## 证据强度分级(决定选不选、给多少倍率、信心几档)
- 最硬: 跨版本一致(同一个格在多个口径版本里都好/都坏) + 两段绝对分与相对分同向 + 笔数充足
- 中等: 单版本内两段同向但样本偏薄, 或绝对分过关而相对分勉强
- 弱(不得作主证据): 年度切片多数 <5 笔 / 只有少数年份有记录 / 全量靠单段爆发拉高
- 【稳定亏的格往往比稳定赚的格证据更硬】— 排除坏格是最可靠的收益来源, 优先确认它

## 路径质量(与 PF 高低同权的第二维 — 高 PF 不等于好格)
只看全量 PF 会把"靠爆发拉高均值"的格当宝。**每个候选格(入选与否都要)必须报路径三数**:
有交易的年份数 / 其中 PF<1 的年份数 / 最低年份 PF(≥5笔的年切片才计, 薄年份另列)。
据此定档(与"证据强度"取更严的一档):
- 路径稳: 逐年 PF **几乎不穿 1**(亏损年 ≤1/4 且最低年 ≥0.8) → 不因方差降档, 可给 1.0;
- 路径不平: 跳动大**但基本不穿 1**(亏损年 ≤1/3 且最低年 ≥0.6) → 期望真实但曲线难看,
  给 0.6~0.8, 且 evidence 里明写"路径不平, 逐年 X/Y 年亏, 最低 Z";
- 爆发型: 亏损年 >1/3, 或最低年 <0.5, 或去掉最好的那 1~2 年后全量 PF 跌破 1
  → **不入选**(哪怕全量 PF 很高)。降倍率只缩小亏损不改变期望虚假, 实盘错过爆发段就是纯亏。
【判据要点】**PF 1.9 且逐年稳 优于 PF 2.2 但半数年份亏** — 前者回撤浅、可上更大仓位、
资金曲线更好; 推荐时必须按此排序, 不许按全量 PF 大小排。

## 第二问: 策略可用的 gate — 在第一问胜出的版本里选格
- 选格评分权重: 近 5 年 ≈ 66%, 更早 ≈ 34%(近期更相关, 远期验证贯穿);
  **但权重作用在"相对分"上** — 有周期漂移时按绝对 PF 加权会选出一堆时代红利格。
- 倍率是**建议值**(人最终拍板), 按证据强度×路径质量给, **不是按 PF 高低给**:
  两维取更严的一档 — 最硬且路径稳 = 1.0, 中等或路径不平 = 0.6~0.8,
  弱或爆发型 = 【不入选】(别用低倍率把证据不足/爆发型的格硬塞进来)。
- 【你的职责是给依据, 不是替人决定】每个入选格必须把"为什么给这个倍率"的数字摊开
  (相对分两段/笔数两段/路径三数/跨版本一致性), 让人能否决你的建议改成别的值;
  同理未入选格也要写清"因为哪个数字不合格", 而不是只说"排除"。

## 表述纪律(人来决策, 所以要写人话 — 违反视同没写)
决策的人不是统计学家, 每个格**先用一句普通人能懂的话下结论, 数字放后面括号里支撑**:
- ✅ 好: "BBA 是这个策略最可靠的天气 —— 二十年里 14 个有交易的年份只有 2 年亏, 最差的一年
  也只小亏, 早年和近五年都稳定赚〔218笔·相对分1.26·路径14/2/0.86·四版本一致〕。建议满仓。"
- ❌ 差: "BBA: 全量218笔PF1.45(相对1.26); 早段184笔1.35/近5年34笔2.13; 路径14/2/0.86 → 路径稳。"
  (同样的信息, 但人要自己在心里翻译一遍)
规则:
1. 每格开头一句白话判断("最可靠的天气" / "赚钱但曲线难看" / "看着漂亮实则靠两年爆发" /
   "样本太少还说不清"), 然后才是数字;
2. 术语必须现场翻译: 相对分→"扣掉大环境变好的红利后还赚"; 路径三数→"X个年份里Y年亏,
   最差那年Z"; 爆发型→"全靠某一两年撑起来"; half_rho/p值→"换个时间段看还成不成立";
3. 不要只丢结论词(强/中/弱、最硬/中等), 要说清"哪里强、哪里不放心";
4. 未入选格也用人话说透为什么("这格一半年份在亏, 平均数是被 2023 年那一波拉高的");
5. 最后给一句**给决策者的话**: 这个门最值得信的一点是什么、最可能翻车的一点是什么。
- 门机制(必须按此理解): 无 gate = 全量交易(每个信号都下单);
  有 gate = 入场日的格在 cells 里才交易, 倍率 0.5~1 缩放仓位;
  数字不好的格【不写进 cells = 那种天气直接不交易】— 不存在负倍率或反着做。

## Regime 原理(铁律: 只描述当天性格, 绝不预测未来)
每交易日由三个二值维度拼成三字母格(8格 AAA..BBB):
第1位 长趋势 / 第2位 短趋势 / 第3位 波动 — 各版本参数见 regime_versions[].params, 无未来函数。

## 信心分级(我们是要有信心分级的探索系统)
confidence: high / medium / unverified — 不确定就降级, 用数字说话。硬性下限:
- 该版本 regularity 为"弱"(无一项 p<0.05) → 最高只能 medium(gate 只许放跨版本一致的格);
- 可复现性两项 p 均 ≥0.05, 或 dispersion 是 separation 数倍 → 最高只能 medium;
- 入选格里有"弱证据"格(样本薄/单段爆发) → 最高只能 medium;
- 只有"最硬"级证据支撑的格 + 三道检验都过 → 才可 high。

## 输出(严格 JSON, 不要多余文字)
【按轮次只填该轮的报告】第一轮: data_check + regime_report(strategy_report 置 null);
第二轮: data_check + strategy_report(regime_report 置 null)。下面是两轮合并的完整字典。
{
  "model": "<你自己的准确模型名, 如 gemini-2.5-pro / claude-opus-4-8 — 会随门入库备注>",
  "data_check": {
    "computed_by": "code|none",
    "probes": {"v1|2008|BAB": [12, 3456.7, 2345.6]},
    "mismatch": null
  },
  "regime_report": {
    "baseline": {"early_pf": 0.98, "recent5y_pf": 1.31, "drift": "整体近5年变好, 选格须用相对分"},
    "ranking": "v1 > v2",
    "pick_reason": "significance|bad_cell_fallback",
    "pick_note": "一句人话: 为什么第一名是它 — 若最高显著性的版本没当第一, 必须在这里说清为什么",
    "versions": [
      {"version": 1, "regularity": "强|中|弱",
       "metrics": {"separation": 0.21, "dispersion": 0.73, "ratio": 0.30, "half_rho": 0.5,
                   "rho_blocks": 0.05, "null_method": "permutation|fixed_threshold",
                   "ratio_z": 0.95, "ratio_p": 0.16, "half_rho_z": 0.43, "half_rho_p": 0.33,
                   "rho_blocks_z": 3.61, "rho_blocks_p": 0.0015, "bonferroni_pass": ["rho_blocks"]},
       "evidence": "三道检验的数字依据(周期剥离后仍成立的格 / 分离与离散 / 半样本排序); 未全量版本注明受限"},
      {"version": 2, "regularity": "强|中|弱", "metrics": {}, "evidence": "..."}
    ]
  },
  "strategy_report": {
    "strategy_id": <id>,
    "recommended_version": <建议版本号, 与 gate.version 一致>,
    "cells_all": [
      {"cell": "AAA", "mult": 0, "n": 452, "pf": 1.15, "rel_early": 0.97, "rel_recent": 1.06,
       "path": [14, 5, 0.41], "grade": "弱|中等|最硬", "path_grade": "稳|不平|爆发型",
       "why": "一句人话: 为什么给这个倍率 / 为什么不选"},
      {"cell": "AAB", "mult": 1.0, "...": "...八个象限一个不漏, 不选的给 mult=0"}
    ],
    "gate": {"regime": {"cells": {"ABA": 1.0, "BBA": 0.7}, "version": <ranking第一名>}},
    "confidence": "high|medium|unverified",
    "ai_regime_recommend": "选哪个 version 及理由(引三道检验的数字与 p 值); 全局周期基准与剥离后的相对分; 每个入选格的绝对分/相对分/两段笔数/路径三数(年份数·亏损年数·最低年PF)与证据等级×路径档→倍率依据; 未入选格=不交易的理由(优先点出跨版本一致的坏格); 月切片仅同向印证; 样本不足处保留意见"
  }
}
- model 如实填你自己的模型名(不确定就写引擎名, 别编版本号);
- **pick_reason 必填**: 靠显著性定名 = "significance"; 走坏格识别兜底 = "bad_cell_fallback"。
  【只有过 Bonferroni 校正的项才算"硬显著"】—— 若某版本有 p<0.05 但未过校正, 它**不足以**
  单独定名次, 此时应走兜底并在 pick_note 里明写"vN 的 pX 未过校正, 名次不是靠显著性定的";
- **rho_blocks 与 half_rho 打架时以 rho_blocks 为准**(它是 6 对块的均值, 比单切点稳健) —
  不要反过来把 rho_blocks 说成"切点巧合";
- **路径三数自检**: 最低年 PF 若八个格算出来全都一样(尤其全是 0), 说明你算错了(多半把 0 笔
  年份或无亏损年当成 0) — 重算或在 why 里注明"路径不可用", 别报出明显失真的数;
- cells_all 必须**八个象限全给**(AAA/AAB/ABA/ABB/BAA/BAB/BBA/BBB), 不选的写 mult=0
  并在 why 里说清哪个数字不合格; path=[有交易年份数, 其中PF<1的年数, 最低年PF];
  gate.cells 只含 mult>0 的格(与系统 metadata 格式逐字节兼容, 系统据此预填面板);
- computed_by 必须如实填: 查表与一切汇总(全量PF/近5年加权等)用代码完成的填 "code",
  没代码工具/心算的填 "none"。【系统只接受 "code"】— 报告里的每个汇总数字都必须是
  代码算出来的; 填 "none" 会被拒收, 但禁止为了过关谎报(探针与抽核对不上照样作废);
- probes 任何情况必填; 只有确实缺数据时才在 mismatch 说明并把两份报告置 null;
- regime_report 评的是【口径】(与策略赚不赚钱无关, 稳定亏也算规律), ranking 给版本排序;
- strategy_report 是【策略可直接使用的配置】: gate 与系统 metadata.regime 格式逐字节兼容;
  没有可信规律时 cells 给 {} 并在 recommend 说明。

## 数据
"""


@router.get("/strategies/{strategy_id}/regime_prompt")
async def regime_ai_prompt(strategy_id: int, request: Request):
    """单策略AI调参·1-Regime 提示词(单轮, 报告分三块: ⓪验收 ①版本评价 ②八象限配置)。
    数据全在库侧预聚合(精简模式, 全部版本的 年×格+月×格), AI 零计算只读表。"""
    pool = request.app.state.pool
    await identity.assert_strategy_visible(pool, request, strategy_id)
    s = await pool.fetchrow(
        "SELECT id, name, template, symbol, timeframe, params, status, parent_id, metadata"
        " FROM strategies WHERE id=$1", strategy_id)
    if s is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    # 自动上溯无门根(2026-08-09 Frank 定): 门变体的成交被门滤过 = 残卷, 评口径必须全量 —
    # 输入门变体 ID 也自动用它的无门根分析(与克隆挂根同一血统逻辑); 产出的 gate 也该配根
    requested_id, req_params, seen = s["id"], s["params"], {s["id"]}
    while isinstance(s["metadata"], dict) and (s["metadata"] or {}).get("regime") \
            and s["parent_id"] and s["parent_id"] not in seen:
        seen.add(s["parent_id"])
        nxt = await pool.fetchrow(
            "SELECT id, name, template, symbol, timeframe, params, status,"
            "       parent_id, metadata FROM strategies WHERE id=$1", s["parent_id"])
        if nxt is None:
            break
        s = nxt
    # 复检门警告(2026-08-09 Frank 定, 门上调参工作流第⑤步防呆): 调参子代 = 新参数+带门,
    # 上溯到的根是【旧参数】的全量 — 用它复检该子代的门是拿旧地图走新路, 必须先造无门同参实例
    warning = None
    if s["id"] != requested_id and s["params"] != req_params:
        warning = (f"#{requested_id} 是调参子代(参数与无门根 #{s['id']} 不同)!"
                   f" 本提示词分析的是根 #{s['id']}【旧参数】的全量交易, 不能用来复检"
                   f" #{requested_id} 的门 — 请先用 #{requested_id} 的参数在生成页建一个"
                   f"无门实例并回测, 再载入那个新 ID")
    bt = await pool.fetchrow(
        "SELECT from_time, to_time, trades FROM backtests"
        " WHERE strategy_id=$1 AND symbol=$2", s["id"], s["symbol"])
    if bt is None:
        raise HTTPException(status_code=400, detail=(
            f"无门根 #{s['id']} 的主品种没有回测行 — 先给它跑一发回测(建议20年)"
            if s["id"] != requested_id else "主品种没有回测行 — 先跑一发回测(建议20年)"))
    # 精简模式(2026-08-09 Frank 定, AI 原话"需要预聚合数据"): 贴格+聚合全部由数据库侧
    # 完成, AI 零计算只读表判断 — "不肯跑代码"类失败整类消失, 数字全部出自库(比 AI 算的可信)。
    # 先日聚合(regime 原生分辨率=天, 同日笔贴同格), 再按版本贴格出 年×格 / 月×格 两张表
    daily: dict = {}
    for t in (bt["trades"] or []):
        d = datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).date()
        a = daily.setdefault(d, [0, 0, 0.0, 0.0])
        pts = float(t.get("points") or 0) * float(t.get("mult") or 1)
        a[0] += 1
        if pts > 0:
            a[1] += 1
            a[2] += pts
        else:
            a[3] -= pts
    cur_vid, _ = await regime.active_version(pool)
    versions = []
    for v in await pool.fetch("SELECT id, params FROM regime_versions ORDER BY id"):
        vid = v["id"]
        try:   # 切谁治谁: 自愈该版本时间线(与矩阵页同规矩)
            await regime.ensure_timeline(pool, s["symbol"], vid)
        except Exception as e:
            logger.warning("regime ensure v%s %s failed: %s", vid, s["symbol"], e)
        tl_rows = await pool.fetch(
            "SELECT date, regime FROM regime_timeline"
            " WHERE version_id=$1 AND symbol=$2 ORDER BY date", vid, s["symbol"])
        tl = {r["date"]: r["regime"] for r in tl_rows}
        ycells: dict = {}   # (年, 格) → [笔, 赢, 毛利, 毛损]
        mcells: dict = {}   # (YYMM, 格) → 同上
        for d, a in daily.items():
            cell = tl.get(d, "unlabeled")
            for acc in (ycells.setdefault((d.year, cell), [0, 0, 0.0, 0.0]),
                        mcells.setdefault((d.strftime("%y%m"), cell), [0, 0, 0.0, 0.0])):
                acc[0] += a[0]
                acc[1] += a[1]
                acc[2] += a[2]
                acc[3] += a[3]
        if not tl_rows:
            cov = "无时间线(未重建) — 该版本无法分析"
        else:
            t0, t1 = tl_rows[0]["date"], tl_rows[-1]["date"]
            full_cov = t0 <= bt["from_time"].date() and t1 >= bt["to_time"].date()
            cov = "全量(覆盖整个回测区间)" if full_cov else (
                f"未全量: 时间线 {t0}~{t1}, 回测 {bt['from_time']:%Y-%m-%d}~"
                f"{bt['to_time']:%Y-%m-%d} — 覆盖外的笔计入 unlabeled, 该版本结论受限")
        versions.append({
            "version": vid, "is_current_default": vid == cur_vid,
            "params": v["params"], "coverage": cov,
            "columns": ["切片", "格", "笔数", "赢笔数", "毛利点", "毛损点"],
            "year_cells": [[y, c, a[0], a[1], round(a[2], 1), round(a[3], 1)]
                           for (y, c), a in sorted(ycells.items())],
            "month_cells": [[m, c, a[0], a[1], round(a[2], 1), round(a[3], 1)]
                            for (m, c), a in sorted(mcells.items())],
        })
    import json as _json

    def _j(obj):
        return _json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)

    base = {"strategy": {"id": s["id"], "name": s["name"], "template": s["template"],
                         "params": s["params"], "symbol": s["symbol"],
                         "timeframe": s["timeframe"], "status": s["status"],
                         **({"note": f"你输入的 #{requested_id} 是门变体(成交被门滤过),"
                                     f" 已自动改用它的无门根 #{s['id']} 的全量回测分析"}
                            if s["id"] != requested_id else {})},
            "backtest_window": f"{bt['from_time']:%Y-%m-%d} ~ {bt['to_time']:%Y-%m-%d}"}
    # 探针(防缺段/略读): 点名5行 年×格, AI 照抄该行 [笔数, 毛利点, 毛损点];
    # 真实答案不进提示词, 单独返回给页面自动验收
    flat = [(v["version"], r) for v in versions
            for r in v["year_cells"] if r[1] != "unlabeled"]
    p_idx = sorted({len(flat) * k // 10 for k in (1, 3, 5, 7, 9)}) if flat else []
    probe_answers = {f"v{flat[i][0]}|{flat[i][1][0]}|{flat[i][1][1]}":
                     [flat[i][1][2], flat[i][1][4], flat[i][1][5]] for i in p_idx}
    base["probe_keys"] = list(probe_answers)
    # 两段式(精简模式): ①预聚合数据(全部版本) ②任务指令+策略身份(压轴带开始口令);
    # slug 用于下载文件名 prompt-{id}-{n}-{slug}.txt(带策略id, 下载不重名)
    parts = [
        {"label": "① 预聚合数据·全部版本 (1/2)", "slug": "data",
         "text": "以下是交易策略 regime 分析的数据, 共 2 段: ①全部 regime 口径版本的预聚合"
                 "战绩(系统已在数据库侧完成贴格与聚合 — 每版本 年×格 / 月×格 两张表) ②任务指令。"
                 "请先暂存, 收到第②段指令后再开始分析。\n\n"
                 + _j({"regime_versions": versions})
                 + "\n\n【第 1/2 段完 — 指令在第②段, 请继续等待】"},
        {"label": "② 任务指令+策略身份 (2/2)", "slug": "instruction",
         "text": _REGIME_AI_PROMPT_HEAD + _j(base)
                 + "\n\n【全部发完 — 以上即任务指令, 数据在第①段, 请开始分析】"},
    ]
    full = "\n\n".join(pt["text"] for pt in parts)
    return {"prompt": full, "parts": parts, "probe_answers": probe_answers,
            "versions": [{"id": v["version"], "label": regime.label(v["params"]),
                          "coverage": v["coverage"]} for v in versions],
            **({"warning": warning} if warning else {})}


@router.get("/strategies/{strategy_id}/report")
async def ai_report(strategy_id: int, request: Request):
    """AI 成绩单(结构化 JSON, 纯数字无评语 — 事实只存一份, 表述现算):
    身份/参数 + 主品种回测(含 oos/by_year/mae/mfe) + 跨品种 + 可信度(对账) + 实盘
    + 同模板尸体(负样本: 参数+死因码)。喂给 AI 生成器做调参迭代的输入。"""
    pool = request.app.state.pool
    await identity.assert_strategy_visible(pool, request, strategy_id)  # 也罩住 trail_prompt/ai_prompt
    await usage.bump_by_owner(pool, "ai_reports", [strategy_id])  # 用量: 只记录不拦截
    s = await pool.fetchrow(
        "SELECT id, name, template, params, symbol, timeframe, status, magic_number,"
        "       archive_reason, metadata FROM strategies WHERE id=$1", strategy_id)
    if s is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    bts = await pool.fetch(
        "SELECT symbol, broker, from_time, to_time, metrics, trades FROM backtests"
        " WHERE strategy_id=$1 ORDER BY (symbol = $2) DESC, symbol", strategy_id, s["symbol"])
    actual = await pool.fetchrow(
        "SELECT sum(trades) AS t, sum(wins) AS w, sum(profit) AS p"
        " FROM strategy_stats WHERE strategy_id=$1", strategy_id)
    dead = await pool.fetch(  # 同模板负样本: 已淘汰的参数 + 生因 + 死因(AI 别再生成同类)
        "SELECT params, basis, archive_reason FROM strategies"
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
    envs = await pool.fetch(     # 实盘按环境拆分(demo/live 各自战绩快照; 多账户按 env 聚合)
        "SELECT env, sum(trades)::int AS trades, sum(wins)::int AS wins,"
        "       sum(profit) AS profit, max(updated_at) AS updated_at"
        " FROM strategy_stats WHERE strategy_id=$1 GROUP BY env ORDER BY env", strategy_id)
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
        "failed_neighbors": [{"params": d["params"], "basis": d["basis"],
                              "died_of": d["archive_reason"]} for d in dead],
    }


# AI 调参的方法论标签: 追加进每个子代的生因备注(与 AI 自报的模型名一起) — 家族溯源用。
# 方法由系统写死(提示词纪律本身就是这套打法), 不让 AI 自由发挥; 模型名 AI 自报, API 接入后改为服务端如实填。
_AI_TUNE_METHOD = "局部搜索+证据驱动"


class AiCandidatesRequest(BaseModel):
    combos: list                 # [{"params": {...}, "basis": "..."}] 或裸参数dict列表
    model: Optional[str] = None  # AI 自报的模型名(协议顶层字段), 追加进生因备注


@router.post("/strategies/{strategy_id}/ai_candidates")
async def ai_candidates(strategy_id: int, req: AiCandidatesRequest, request: Request):
    """AI 调参收货(v2.2 步骤2, 只生成不回测 — 回测由用户手动按ID触发):
    指定模板(=父策略的模板) + 参数list → 逐组校验 → parent_id 谱系入库 → 逐组反馈 + 回读核验。
    每组返回: 新ID(created) / 已存在ID(existing) / 不合格原因(error);
    created 的做"回读核验": 从库里读回 params 与请求逐字段比对, verified=true 才算数。"""
    pool = request.app.state.pool
    parent = await pool.fetchrow(
        "SELECT id, template, symbol, timeframe, metadata FROM strategies WHERE id=$1",
        strategy_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    # 与生成入口同一守门: 品种必须仍在登记中(父创建后可能被除名, 不给孤儿品种生子代)
    if not await pool.fetchval("SELECT 1 FROM symbols WHERE symbol=$1", parent["symbol"]):
        raise HTTPException(
            status_code=400,
            detail=f"品种 {parent['symbol']} 已除名 — 先在下载页重新登记再生成子代")
    # 生因备注统一加尾标〔方法 · 模型〕: AI 依据原句 + 谁按什么方法生成的, 一并入库
    tag = f"〔{_AI_TUNE_METHOD}" + (f" · {req.model}" if req.model else "") + "〕"
    combos = [{**c, "basis": f"{c['basis']} {tag}" if c.get("basis") else tag}
              if isinstance(c, dict) and "params" in c else {"params": c, "basis": tag}
              for c in req.combos]
    # 子代继承门(2026-08-09 Frank 定, 门上调参工作流): 父带门 → 子代同门+同款名字后缀
    # (与克隆带门一致), 家族对比三铁律天然满足; 无门父 = metadata None, 行为不变
    g = (parent["metadata"] or {}).get("regime") \
        if isinstance(parent["metadata"], dict) else None
    md, suffix = None, ""
    if g:
        md = {"regime": g}
        suffix = f"-gate-v{g['version']}-" + "-".join(
            f"{k}{float(g['cells'][k]):g}" for k in sorted(g["cells"]))
    # 与生成页同一条收货管道(services.instances), 只多带 parent_id 谱系
    r = await instances.create_instances(
        pool, parent["template"], parent["symbol"], parent["timeframe"],
        combos, parent_id=strategy_id, metadata=md, name_suffix=suffix)
    return {**r, "template": parent["template"], "symbol": parent["symbol"],
            "timeframe": parent["timeframe"],
            **({"inherited_gate": g} if g else {})}


@router.get("/strategies/{strategy_id}/family")
async def family(strategy_id: int, request: Request):
    """谱系对比: 父策略 + 全部 AI 子代, 各带主品种回测成绩(净点/PF/OOS留出/MAE) — AI分析页对比表"""
    await identity.assert_strategy_visible(request.app.state.pool, request, strategy_id)
    rows = await request.app.state.pool.fetch(
        "SELECT s.id, s.name, s.params, s.status, s.archive_reason, s.parent_id,"
        "       s.basis, s.created_at, b.metrics"
        " FROM strategies s"
        " LEFT JOIN backtests b ON b.strategy_id = s.id AND b.symbol = s.symbol"
        " WHERE s.id = $1 OR s.parent_id = $1"
        " ORDER BY (s.id = $1) DESC, s.id", strategy_id)
    out = []
    for r in rows:
        m = r["metrics"] or {}
        oos = (m.get("oos") or {}).get("holdout") or {}
        out.append({
            "id": r["id"], "name": r["name"], "params": r["params"], "status": r["status"],
            "archive_reason": r["archive_reason"], "basis": r["basis"],
            "is_parent": r["id"] == strategy_id,
            "created_at": r["created_at"],
            "trades": m.get("trades"), "net_points": m.get("net_points"),
            "profit_factor": m.get("profit_factor"), "max_dd_points": m.get("max_dd_points"),
            "holdout_net": oos.get("net_points"), "holdout_trades": oos.get("trades"),
            "mae_p90": m.get("mae_p90"), "mfe_p90": m.get("mfe_p90"),
        })
    return {"family": out}


_AI_TUNE_PROMPT = """你是量化策略调参助手。下面给出策略 #{sid} 的完整成绩单(JSON, 含回测逐笔/实盘/对账校准/同模板尸体)。

模板 {template} 的参数空间(每个参数: [最小, 最大, 步长]):
{space}

注意: 若 strategy.metadata.regime 存在 = 本策略带 regime 门, 成绩单里的交易已按门过滤
(入场日的格在 cells 内才交易) — 你优化的就是门内表现, 新参数实例将继承同一个门。

任务: 基于成绩单证据, 提出 {count} 组新参数做下一轮回测。纪律:
1. 每组相对当前参数({params})最多改 2 个维度, 且必须落在参数空间范围内、按步长对齐
2. 每组必须附 "basis": 一句依据, 引用成绩单里的具体数字(如 MAE 分布/方向不对称/时段/留出段)
3. 避开 failed_neighbors 里已死亡的参数区域
4. 共 {count} 组, 宁少勿滥; 变化方向要聚焦(围绕最有证据的1~2个假设), 不要均匀撒网

数据完整性探针(防略读, 答错任何一个 = 整份作废): 在主品种回测 trades.rows 里找到
entry_time 等于下列值的行: {probe_times} — 返回 JSON 顶层必须带
"data_check": {{"computed_by": "code|none", "probes": {{"<entry_time>":
[该行的points, 该行的reason], …}}}}。
定位方式: 用代码工具解析 trades.rows 按 entry_time 取行(推荐), 或文本搜索该数字命中行。
computed_by 必须如实填: 用代码工具实际解析核对的填 "code", 没代码工具/没跑的填 "none"。
【系统只接受 "code"】— 填 "none" 会被拒收; 但禁止为了过关谎报 "code"(谎报是最严重违规,
答案对不上照样拒收)。probes 两个值从该行原样照抄, 不许为 null;
系统持有真实答案逐一核对, 答错或缺失 = 你没读成绩单, 整份拒收。

返回格式(协议, 系统会机器解析。严格遵守):
- 只输出一个 JSON 对象: 第一个字符必须是 {{, 最后一个字符必须是 }}
- 不要 markdown 代码围栏(```), 不要任何前言/解释/结尾文字
- combos 恰好 {count} 项; 每组 params 的键必须恰好是 {param_keys}, 值为具体数字(不是区间/占位符)
- template 按下面的值原样带回(系统核对用); model 填你自己的准确模型名(如 claude-opus-4-8,
  入库记在每个实例的生因备注里); 除 template/model/data_check/combos 外不要其他顶层字段
- 标准 JSON: 双引号、无尾逗号、无注释

结构(params 各键取值范围): {{"template": "{template}", "model": "<你的模型名>", "data_check": {{"computed_by": "code|none", "probes": {{"<entry_time>": [points, reason]}}}}, "combos": [{{"params": {params_schema}, "basis": "一句依据"}}]}}
已填好的单组示例(仅示范格式, 数值别照抄): {{"template": "{template}", "model": "claude-opus-4-8", "combos": [{{"params": {params_example}, "basis": "sl出场148笔合计-201874, 收窄止损换结构改善"}}]}}

成绩单:
{report}

(再次强调: 你的全部输出 = 一个 JSON 对象, 以 {{ 开头以 }} 结尾, combos 恰好 {count} 项,
顶层带 data_check.probes 抽查答案, 无围栏无解释。)"""


@router.get("/strategies/{strategy_id}/ai_prompt")
async def ai_tune_prompt(strategy_id: int, request: Request, count: int = 10):
    """AI 调参提示词(单一来源): 指令纪律 + 参数空间 + 完整成绩单。
    页面第1步展示/复制、prompt.txt、ai_propose(一键问AI) 都取这里, 三处永远同文。"""
    import json as _json
    report = await ai_report(strategy_id, request)
    meta = report["strategy"]
    cls = TEMPLATES[meta["template"]]
    space = cls.RANDOM_SPACE or cls.PARAM_GRID
    # params 骨架: 逐键点名类型与范围, 让 AI 照抄键名只填数字; 另给一组当前参数当填好的示例
    params_schema = "{" + ", ".join(
        (f'"{k}": <{v[0]}~{v[1]}, 步长{v[2]}>' if isinstance(v, tuple) else f'"{k}": <候选 {v}>')
        for k, v in space.items()) + "}"
    # 探针抽查(2026-08-09 诚实条款, 与 AI·Regime 页同机制): 点名5笔的 entry_time(全局唯一,
    # 粘贴/附件场景都能文本搜索定位, 不逼 AI 数行号), AI 回报该行 points/reason;
    # 答案单独返回, 页面解析预览自动核对拦截
    main_rows = next((b["trades"]["rows"] for b in report["backtests"]
                      if b.get("is_main") and b.get("trades")), [])
    probe_idx = sorted({len(main_rows) * k // 10 for k in (1, 3, 5, 7, 9)}) \
        if main_rows else []
    probe_answers = {str(main_rows[i][0]): [main_rows[i][5], main_rows[i][6]]
                     for i in probe_idx}
    # 提示词瘦身(2026-08-09 Frank 定, 只瘦提示词不动 /report 原始档案):
    # 逐笔不可聚合(MAE/MFE/出场原因是调参主证据), 砍列 — 去 exit_time/entry/exit
    # 三列价格(点数已在 points, 调参用不上), 9列→6列约省 35%; JSON 再用紧凑分隔符
    for b in report["backtests"]:
        t = b.get("trades")
        if t:
            t["cols"] = ["entry_time", "dir", "points", "reason", "mae", "mfe"]
            t["rows"] = [[r[0], r[2], r[5], r[6], r[7], r[8]] for r in t["rows"]]
    prompt = _AI_TUNE_PROMPT.format(
        sid=strategy_id, template=meta["template"],
        space=_json.dumps(space, ensure_ascii=False),
        params=_json.dumps(meta["params"], ensure_ascii=False),
        params_schema=params_schema,
        params_example=_json.dumps(meta["params"], ensure_ascii=False),
        param_keys=_json.dumps(sorted(space), ensure_ascii=False),
        count=count,
        probe_times=_json.dumps([main_rows[i][0] for i in probe_idx]),
        report=_json.dumps(report, ensure_ascii=False, default=str,
                           separators=(",", ":")))
    return {"prompt": prompt, "space": space, "strategy": meta,
            "probe_answers": probe_answers}


_TRAIL_TUNE_PROMPT = """你是移动止损(trailing stop)调优助手。下面给出策略 #{sid} 的完整成绩单(JSON)。
本轮只调 trailing 插件, 策略本身参数一律原样保留、一个都不许改。

trailing 配置结构(params 里的 "trail" 键):
  {{"active": "fixed"|"breakeven"|"atr",
   "fixed":     {{"gap": <点数>}},                  // SL 跟最高价固定距离
   "breakeven": {{"gap": <点数>, "start": <点数>}}, // 盈利达 start(点)才启动, 先保本再追
   "atr":       {{"k": <倍数>, "period": 14}}}}     // 距离=M1 ATR(period)×k, 自适应

已验证的先验(必须遵守):
- start 是命门: 开仓即贴身追会掐死行情。fixed/breakeven 的 gap 取该策略止损距离的 0.5~1.0 倍;
  breakeven 的 start 取止损距离的 1~2 倍; atr 的 k 在 2(贴身,已证差)~8(名存实亡)之间, 4~5 曾是平台区
- trailing 克"利润靠少数大单"型(笔数少/单笔集中度高), 亲"利润分散高频"型 —
  成绩单里笔数少且 top_trade_pct 高时, 倾向宽松档并在 basis 说明风险
- 评判以样本外留出段为准, 全样本好看没有用

任务: 提出 {count} 组 trail 配置, 三类硬性均分 — fixed 恰好 {n_fixed} 组、
breakeven 恰好 {n_be} 组、atr 恰好 {n_atr} 组(这样结果表能对比出哪类适合本策略),
每类内部数值拉开梯度(结合成绩单的 MAE/MFE 分布与持仓形态选数值), 每组 basis 写清依据。
参考: 该策略当前参数 {params}(只供你估算止损尺度, 不要出现在返回里)。

返回格式(协议, 系统机器解析, 严格遵守):
- 只输出一个 JSON 对象: 第一个字符 {{, 最后一个字符 }}; 无 markdown 围栏、无前言后语
- trails 恰好 {count} 项; 每项只有 "trail"(配置对象) 和 "basis"(一句依据, 引用成绩单数字)
- model 填你的准确模型名; 顶层只有 model/trails — 策略参数不出现在返回里(系统只解析 trail)

结构: {{"model": "<你的模型名>", "trails": [{{"trail": {{"active": "…", …}}, "basis": "一句依据"}}]}}

成绩单:
{report}

(再次强调: 输出=一个 JSON 对象, trails 恰好 {count} 项, 只有 trail 配置、不含任何策略参数。)"""


@router.get("/strategies/{strategy_id}/trail_prompt")
async def trail_tune_prompt(strategy_id: int, request: Request, count: int = 21):
    """插件调优提示词(第4步, 与生成策略完全分开): 协议只返回 trail 配置(策略参数不出现在
    返回里, 协议层面杜绝混线)。配套 trail_batch 内存批跑 + 留出段裁判, 「保留」写回本策略。"""
    import json as _json
    report = await ai_report(strategy_id, request)
    meta = report["strategy"]
    n_fixed = (count + 2) // 3   # 三类硬性均分(20 → 7/7/6): 结果表按类对比哪种适合本策略
    n_be = (count + 1) // 3
    prompt = _TRAIL_TUNE_PROMPT.format(
        sid=strategy_id, template=meta["template"], count=count,
        n_fixed=n_fixed, n_be=n_be, n_atr=count - n_fixed - n_be,
        params=_json.dumps(meta["params"], ensure_ascii=False),
        report=_json.dumps(report, ensure_ascii=False, default=str))
    return {"prompt": prompt, "strategy": meta}


def _holdout(trades: list, split: float = 0.7):
    """70/30 留出段(裁判位): 按入场时间切, 返回(留出净点, 留出回撤)。与 OOS 口径一致"""
    if not trades:
        return 0.0, 0.0
    t0, t1 = trades[0]["entry_time"], trades[-1]["entry_time"]
    cut = t0 + split * (t1 - t0)
    net = eq = pk = mx = 0.0
    for t in trades:
        if t["entry_time"] < cut:
            continue
        net += t["points"]
        eq += t["points"]
        pk = max(pk, eq)
        mx = max(mx, pk - eq)
    return round(net, 1), round(mx, 1)


class TrailBatchRequest(BaseModel):
    trails: list   # [{"trail": {...}, "basis": "..."}] — 第4步协议, 只有 trail 无策略参数


@router.post("/strategies/{strategy_id}/trail_batch")
async def trail_batch(strategy_id: int, req: TrailBatchRequest, request: Request):
    """插件调优批跑(第4步): 对本策略内存跑 N 版 trail + 基准(M1 只载一次),
    每版附留出段裁判列。不建实例、不落库 — 「保留」由前端调 /trail 写回本策略。"""
    if not req.trails or len(req.trails) > 30:
        raise HTTPException(status_code=400, detail="trails 须为 1~30 组")
    pool = request.app.state.pool
    await identity.assert_strategy_visible(pool, request, strategy_id)
    s = await pool.fetchrow(
        "SELECT s.template, s.params, s.symbol, s.timeframe, sym.point FROM strategies s"
        " LEFT JOIN symbols sym ON sym.symbol = s.symbol WHERE s.id=$1", strategy_id)
    if s is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    if not s["point"] or s["timeframe"] not in backtest.TF_SECONDS:
        raise HTTPException(status_code=400, detail="品种未登记或周期不支持")
    w_from, w_to = await _trail_window(pool, strategy_id, s["symbol"])
    m1 = await backtest.load_m1(pool, s["symbol"], w_from, w_to)
    if m1 is None:
        raise HTTPException(status_code=400, detail=f"{s['symbol']} 无 M1 数据, 先去下载")
    cfg = await pool.fetchval("SELECT value FROM config WHERE key='backtest_costs'") or {}
    costs = {"slippage_points": cfg.get("slippage_points", backtest.DEFAULT_SLIPPAGE_POINTS),
             "commission_points": cfg.get("commission_points", backtest.DEFAULT_COMMISSION_POINTS),
             "spread_points": cfg.get("spread_points")}
    point = float(s["point"])
    base_params = dict(s["params"] or {})
    base_params.pop("trail", None)

    async def _run(p):
        res = await asyncio.to_thread(backtest.run_backtest, m1, s["template"], p,
                                      point, s["timeframe"], oos_split=None, **costs)
        mtr = res["metrics"]
        ho_net, ho_dd = _holdout(res["trades"])
        return {"trades": mtr.get("trades"), "net_points": mtr.get("net_points"),
                "win_rate": mtr.get("win_rate"), "profit_factor": mtr.get("profit_factor"),
                "max_dd_points": mtr.get("max_dd_points"),
                "holdout_net": ho_net, "holdout_dd": ho_dd,
                "tsl": sum(1 for x in res["trades"]
                           if str(x.get("reason", "")).startswith("tsl"))}

    baseline = await _run(base_params)
    rows = []
    for i, it in enumerate(req.trails):
        tr = (it or {}).get("trail") if isinstance(it, dict) else None
        err = instances.trail_error(tr)
        if err:
            rows.append({"i": i + 1, "trail": tr,
                         "basis": (it or {}).get("basis") if isinstance(it, dict) else None,
                         "error": err})
            continue
        r = await _run({**base_params, "trail": tr})
        rows.append({"i": i + 1, "trail": tr, "basis": it.get("basis"), **r})
    return {"strategy_id": strategy_id, "baseline": baseline, "rows": rows}


# 淘汰死因码(schema/022): AI 负样本("这类参数死于什么"), 页面按码翻中文, 不收自由文本
ARCHIVE_REASONS = {"manual", "holdout_loss", "min_trades", "low_pf", "recon_fail",
                   "orphan_symbol", "regime_unstable", "other"}


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
