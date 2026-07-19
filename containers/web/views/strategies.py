"""策略组页面: 列表(index) / 生成+MQ5转化(generate_page) / 分析(analysis, 骨架) / 状态流转
UI 拆分(2026-07-13): 生成=进货(偶发), 列表=日常主战场, 各自成页; 导航挂「策略▾」下拉。"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("strategies", __name__, url_prefix="/strategies")

TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"]


@bp.get("/")
def index():
    """策略列表排名(唯一工作台): 全部策略(含未回测, 成绩为空沉底) + 成绩/评分/健壮性
    + 筛选(品种/券商/状态/多条件)/搜索/排名参数模板。数据走 /backtest/top(LEFT JOIN 版)。"""
    a = request.args
    template = a.get("template") or None
    symbol = a.get("symbol") or None
    broker = a.get("broker") or None
    status = a.get("status") or None
    q_field = a.get("q_field") or "name"
    q_text = a.get("q_text") or None
    min_trades = a.get("min_trades", 0, type=int)
    min_actual_trades = a.get("min_actual_trades", 0, type=int)  # 实盘笔数≥(demo+live合计)
    filters = {k: a.get(k, type=float)
               for k in ("min_win_rate", "min_pf", "max_dd", "min_robust")}
    positive = a.get("positive") == "1"
    oos = a.get("oos") == "1"  # 留出段盈利过滤(OOS 一票否决)
    rank = a.get("rank") or ""  # 排名参数模板名, 空=默认(净点数)
    page = max(a.get("page", 1, type=int), 1)  # 服务端分页页码(1起)
    results, rank_templates, brokers, symbols, templates = [], [], [], [], []
    volume_presets = []  # 唯一源=config表(schema/030种子); api不可用即空, 不用写死值顶(铁律欠账4)
    volume_default = None
    oos_split = 0.7  # 样本外训练段占比(配置页可改), 供页面显示"训练:留出"比例
    total, page_size = 0, 100
    try:
        cfg = api.get("/config")["config"]
        rank_templates = cfg.get("ranking_templates", [])
        oos_split = cfg.get("backtest_oos_split", 0.7)
        page_size = cfg.get("ranking_page_size", 100)  # 排名页每页条数(config可改, 缺省100)
        volume_presets = cfg.get("volume_presets") or []
        volume_default = cfg.get("volume_default")
        templates = sorted(api.get("/strategies/templates")["templates"].keys())
        params = {"min_trades": min_trades, "limit": page_size, "page": page}
        if min_actual_trades:
            params["min_actual_trades"] = min_actual_trades
        for k, v in (("template", template), ("symbol", symbol),
                     ("broker", broker), ("status", status)):
            if v:
                params[k] = v
        params.update({k: v for k, v in filters.items() if v is not None})
        if positive:
            params["positive_only"] = "true"
        if oos:
            params["oos_pass"] = "true"
        if rank:
            params["rank_template"] = rank
        if q_text:  # 服务端搜索: 策略名模糊 / ID·周期·状态精准
            params["q_field"] = q_field
            params["q_text"] = q_text
        resp = api.get("/backtest/top", **params)
        results = resp["results"]
        total = resp.get("total", len(results))
        syms = api.get("/symbols")["symbols"]
        symbols = [s["symbol"] for s in syms if s.get("download")]
        brokers = sorted({s["broker"] for s in syms if s.get("broker")})
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    total_pages = max((total + page_size - 1) // page_size, 1)  # 向上取整
    base_args = {k: v for k, v in a.items() if k != "page"}     # 翻页链接保留其它筛选
    return render_template("strategies.html", results=results, volume_presets=volume_presets,
                           volume_default=volume_default,
                           symbol=symbol, broker=broker, min_actual_trades=min_actual_trades,
                           status=status, min_trades=min_trades, q_field=q_field, q_text=q_text,
                           filters=filters, positive=positive, oos=oos, rank=rank,
                           rank_templates=rank_templates, brokers=brokers, symbols=symbols,
                           template=template, templates=templates, oos_split=oos_split,
                           page=page, page_size=page_size, total=total,
                           total_pages=total_pages, base_args=base_args)


@bp.get("/generate")
def generate_page():
    """策略生成 + MQ5 转化(造新策略的入口)"""
    templates, mq5_imports, default_symbols = {}, [], ""
    try:
        templates = api.get("/strategies/templates")["templates"]
        mq5_imports = api.get("/strategies/mq5")["imports"]
        # 品种默认值从主档取(download=✓), 不写死 — 登记/删品种自动跟着变
        default_symbols = ",".join(
            s["symbol"] for s in api.get("/symbols")["symbols"] if s.get("download"))
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("strategy_generate.html", templates=templates,
                           mq5_imports=mq5_imports, timeframes=TIMEFRAMES,
                           default_symbols=default_symbols)


@bp.post("/<int:strategy_id>/set-volume")
def set_volume(strategy_id: int):
    """设置每策略下单手数(空=清除, runner 回落 env 默认); runner 下一轮拉取即生效"""
    raw = request.form.get("volume", "").strip()
    try:
        vol = float(raw) if raw else None
        r = api.post(f"/strategies/{strategy_id}/volume", {"volume": vol})
        flash(f"#{strategy_id} 手数 → {r['volume'] if r['volume'] is not None else '默认(worker env)'}"
              " — runner 下一轮生效", "ok")
    except ValueError:
        flash("手数必须是数字, 或留空=用默认", "error")
    except api.ApiError as e:
        flash(f"设置失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))


@bp.get("/analysis")
def analysis():
    """策略分析: 关2对账(输入策略id → 回测 vs 实盘 match%); v1.4 更多归因维度待建"""
    sid = request.args.get("strategy_id", type=int)
    a_symbol = request.args.get("symbol") or None   # 归因看哪个品种的回测(默认主品种)
    recon, ana = None, None
    if sid:
        try:
            recon = api.get(f"/reconcile/{sid}")     # 对账恒用主品种(实盘只在主品种交易)
        except api.ApiError as e:
            flash(f"对账失败: {e}", "error")
        try:
            ana = api.get(f"/analysis/{sid}", **({"symbol": a_symbol} if a_symbol else {}))
        except api.ApiError as e:
            flash(f"分析失败: {e}", "error")
    return render_template("strategy_analysis.html", recon=recon, ana=ana, sid=sid)


@bp.get("/analysis/fragment")
def analysis_fragment():
    """AJAX 片段: 只渲染胜负归因 body(切换回测品种时不刷新整页)"""
    sid = request.args.get("strategy_id", type=int)
    a_symbol = request.args.get("symbol") or None
    ana = None
    if sid:
        try:
            ana = api.get(f"/analysis/{sid}", **({"symbol": a_symbol} if a_symbol else {}))
        except api.ApiError:
            ana = None
    return render_template("_attribution_body.html", ana=ana)


@bp.get("/<int:strategy_id>/report.json")
def ai_report(strategy_id: int):
    """AI 成绩单 JSON 透传(浏览器/未来 AI 训练脚本直接下载; api 内网名浏览器够不到)"""
    try:
        return api.get(f"/strategies/{strategy_id}/report")
    except api.ApiError as e:
        return {"error": str(e)}, 502


def _ai_context(sid: int, count: int):
    """AI 页公共上下文。数据源全部复用, 无本页私货:
    成绩单 = /strategies/{id}/report(与「策略分析」页 AI成绩单JSON 同一个, 那边改这里自动跟)
    提示词 = api /strategies/{id}/ai_prompt(单一来源, prompt.txt 也取它)"""
    import json as _json
    report = api.get(f"/strategies/{sid}/report")
    report_json = _json.dumps(report, ensure_ascii=False, indent=1, default=str)
    info = api.get(f"/strategies/{sid}/ai_prompt", count=count)
    family = api.get(f"/strategies/{sid}/family")["family"]
    return info["prompt"], family, info["strategy"], info["space"], report_json


@bp.get("/ai")
def ai_page():
    """AI 策略分析(v2.2, 全手动分步): ①拿提示词 ②粘参数→生成子代(逐组反馈+核验)
    ③手动按ID回测 ④家族对比→用最优继续。准备工作(下载/重跑回测)先手动做好。"""
    sid = request.args.get("strategy_id", type=int)
    count = request.args.get("count", 10, type=int)
    prompt, family, meta, space, report_json = "", [], None, {}, ""
    if sid:
        try:
            prompt, family, meta, space, report_json = _ai_context(sid, count)
        except (api.ApiError, KeyError) as e:
            flash(f"取成绩单失败: {e}", "error")
    return render_template("strategy_ai.html", sid=sid, count=count, prompt=prompt,
                           family=family, meta=meta, space=space, report_json=report_json)


@bp.get("/ai/prompt.txt")
def ai_prompt_txt():
    """纯文本提示词透传(api 单一来源; scripts/ai_tune.py 等自动化取这里)"""
    sid = request.args.get("strategy_id", type=int)
    count = request.args.get("count", 10, type=int)
    try:
        r = api.get(f"/strategies/{sid}/ai_prompt", count=count)
        return r["prompt"], 200, {"Content-Type": "text/plain; charset=utf-8"}
    except (api.ApiError, KeyError) as e:
        return f"error: {e}", 502, {"Content-Type": "text/plain; charset=utf-8"}


@bp.post("/ai/create")
def ai_create_instances():
    """第3步预览确认后的「创建策略」(AJAX): 解析过的 combos → api 统一收货管道
    (ai_candidates: 三层校验/parent_id谱系/去重/回读核验) → 逐组回执 + created_ids"""
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("strategy_id")
    combos = data.get("combos")
    if not sid or not isinstance(combos, list) or not combos:
        return {"error": "缺 strategy_id 或 combos"}, 400
    try:
        return api.post(f"/strategies/{sid}/ai_candidates",
                        {"combos": combos, "model": data.get("model")})
    except api.ApiError as e:
        return {"error": str(e)}, 502


@bp.post("/ai/submit")
def ai_submit():
    """步骤2 收货: 粘贴 AI 参数 JSON → api 逐组校验入库(parent_id) → 结果表就地渲染(不跳转)。
    每组反馈 新ID/已存在ID/错误原因 + 回读核验(库里参数与请求逐字段一致)。只生成不回测。"""
    import json as _json
    sid = request.form.get("strategy_id", type=int)
    count = request.form.get("count", 10, type=int)
    step2, ids_csv = None, ""
    try:
        payload = _json.loads(request.form.get("combos_json", ""))
        combos = payload.get("combos", payload) if isinstance(payload, dict) else payload
        model = payload.get("model") if isinstance(payload, dict) else None
        step2 = api.post(f"/strategies/{sid}/ai_candidates",
                         {"combos": combos, "model": model})
        ids_csv = ",".join(map(str, step2["created_ids"]))
        n_ok = len(step2["created_ids"])
        n_bad = sum(1 for r in step2["results"] if r.get("error"))
        flash(f"步骤2完成: 新建 {n_ok} 个 · 已存在 "
              f"{len(step2['results']) - n_ok - n_bad} 个 · 不合格 {n_bad} 个 — 明细见下表",
              "ok" if n_ok else "error")
    except _json.JSONDecodeError:
        flash("粘贴内容不是合法 JSON — 确认 AI 只输出了 JSON 本体", "error")
    except (api.ApiError, KeyError, TypeError) as e:
        flash(f"提交失败: {e}", "error")
    prompt, family, meta, space, report_json = "", [], None, {}, ""
    try:
        prompt, family, meta, space, report_json = _ai_context(sid, count)
    except (api.ApiError, KeyError):
        pass
    return render_template("strategy_ai.html", sid=sid, count=count, prompt=prompt,
                           family=family, meta=meta, space=space, report_json=report_json,
                           step2=step2, ids_csv=ids_csv)


@bp.post("/ai/backtest")
def ai_backtest():
    """按ID回测(创建结果里的「回测这批」按钮/表单共用)— 与「策略回测」页同一 api 入口。
    AJAX(X-Requested-With: fetch)返回 JSON 就地显示; 表单提交走 flash+重定向。"""
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    sid = request.form.get("strategy_id", type=int)
    ids = [s.strip() for s in request.form.get("ids", "").split(",") if s.strip()]
    try:
        payload = {"strategy_ids": [int(s) for s in ids]}
        if request.form.get("cross_symbol") == "on":
            payload["cross_symbol"] = True
        api.post("/backtest/run", payload)
        if is_fetch:
            return {"started": len(ids)}
        flash(f"回测已启动: {len(ids)} 个策略 — 跑完后重新「载入」看家族对比", "ok")
    except (api.ApiError, ValueError) as e:
        if is_fetch:
            return {"error": str(e)}, 502
        flash(f"回测启动失败: {e}", "error")
    return redirect(url_for("strategies.ai_page", strategy_id=sid))


@bp.get("/quality")
def quality():
    """回测质量分析: 反过拟合工具箱概览(OOS/健壮/邻域); 关2对账已移到「策略分析」页"""
    return render_template("strategy_quality.html")


@bp.post("/generate")
def generate():
    try:
        result = api.post("/strategies/generate", {
            "template": request.form["template"],
            "symbols": [s.strip().upper() for s in request.form["symbols"].split(",") if s.strip()],
            "timeframe": request.form["timeframe"],
            "mode": request.form.get("mode", "random"),
            "count": request.form.get("count", 50, type=int),
        })
        msg = f"已生成 {result['created']} 个策略实例"
        if result.get("skipped"):
            msg += f"（跳过 {result['skipped']} 个已存在的相同组合）"
        if result.get("truncated"):
            msg += (f"；超出单批收货上限 {result['batch_limit']}，截断 {result['truncated']} 组未处理"
                    f" — 需要更大批量去「配置·策略参数」调大上限")
        flash(msg, "ok" if result["created"] else "error")
    except (api.ApiError, KeyError) as e:
        flash(f"生成失败: {e}", "error")
    return redirect(url_for("strategies.index", status="CANDIDATE"))


@bp.post("/<int:strategy_id>/backtest")
def run_backtest(strategy_id: int):
    """单策略回测 (成本用系统默认; 结果在回测页排名可见)"""
    try:
        api.post("/backtest/run", {"strategy_ids": [strategy_id]})
        flash(f"策略 #{strategy_id} 回测已启动, 结果见回测页", "ok")
    except api.ApiError as e:
        flash(f"回测启动失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))


@bp.post("/archive")
def archive_batch():
    """按【填入的ID】批量淘汰归档 — 标 ARCHIVED, 可逆, 不删除。只处理明确列出的ID,
    与排名页的查看筛选无关(防误伤); 真金(LIVE)/已淘汰归档由 api 侧自动跳过。"""
    ids = [s.strip() for s in request.form.get("strategy_ids", "").split(",") if s.strip()]
    if not ids:
        flash("请填入要淘汰归档的策略ID(逗号分隔)", "error")
        return redirect(request.referrer or url_for("strategies.index"))
    try:
        r = api.post("/strategies/archive", {"strategy_ids": [int(s) for s in ids],
                                             "reason": request.form.get("reason", "manual")})
        msg = f"已淘汰归档 {r['archived']} 条(可逆, 随时可改回)"
        skipped = r["requested"] - r["archived"]
        if skipped:
            msg += f"；跳过 {skipped} 条(真金不动 / 已淘汰归档)"
        flash(msg, "ok" if r["archived"] else "error")
    except (api.ApiError, ValueError) as e:
        flash(f"批量淘汰归档失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))


@bp.post("/mq5")
def mq5_submit():
    try:
        result = api.post("/strategies/mq5", {
            "name": request.form["name"].strip(),
            "source": request.form["source"],
        })
        flash(f"MQ5 已提交待评估 (id={result['id']})", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"提交失败: {e}", "error")
    return redirect(url_for("strategies.generate_page"))  # MQ5 转化表在生成页


@bp.post("/<int:strategy_id>/status")
def set_status(strategy_id: int):
    is_fetch = request.headers.get("X-Requested-With") == "fetch"  # AJAX 原地更新, 不刷新页面
    try:
        result = api.post(f"/strategies/{strategy_id}/status",
                          {"status": request.form["status"]})
        if is_fetch:
            return result
        flash(f"{result['name']} → {result['status']}"
              + (f" (magic={result['magic_number']})" if result.get("magic_number") else ""), "ok")
    except (api.ApiError, KeyError) as e:
        if is_fetch:
            return {"error": str(e)}, 400
        flash(f"状态修改失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))
