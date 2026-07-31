"""策略组页面: 列表(index) / 生成+MQ5转化(generate_page) / 分析(analysis, 骨架) / 状态流转
UI 拆分(2026-07-13): 生成=进货(偶发), 列表=日常主战场, 各自成页; 导航挂「策略▾」下拉。"""
from datetime import datetime

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
    visibility = a.get("visibility") or None
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
    mounts_view = {}     # 挂载列: {sid: {rows: [启用挂载], addable: [可加挂的同角色主机]}}
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
                     ("broker", broker), ("status", status), ("visibility", visibility)):
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
        # 挂载列(v5.0-B2): 整页一次取挂载 + 可加挂的同角色主机(在 python 组好, 模板零逻辑)
        if results:
            mnt = api.get("/strategies/mounts",
                          ids=",".join(str(r["strategy_id"]) for r in results))["mounts"]
            hosts = [h for h in api.get("/hosts")["hosts"]
                     if h.get("enabled") and h.get("runner")]
            for r in results:
                rows_m = [x for x in mnt.get(str(r["strategy_id"]), []) if x["enabled"]]
                role = (r.get("status") or "").lower()
                used = {x["host_id"] for x in rows_m}
                mounts_view[str(r["strategy_id"])] = {
                    "rows": rows_m,
                    "addable": ([h for h in hosts if h["runner"] == role
                                 and h["id"] not in used]
                                if role in ("demo", "live") else []),
                }
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    total_pages = max((total + page_size - 1) // page_size, 1)  # 向上取整
    base_args = {k: v for k, v in a.items() if k != "page"}     # 翻页链接保留其它筛选
    return render_template("strategies.html", results=results, volume_presets=volume_presets,
                           volume_default=volume_default, mounts_view=mounts_view,
                           symbol=symbol, broker=broker, min_actual_trades=min_actual_trades,
                           status=status, visibility=visibility,
                           min_trades=min_trades, q_field=q_field, q_text=q_text,
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


def _regime_lines(ana) -> dict:
    """八格战绩显示行(v2.5 第五步): {格子: [行1, 行2]} — 喂给 m.regime_grid(与 Regime 页同一张图)。
    格内笔数 <20 = 样本不足未证实(灰显), 与全局小样本纪律一致"""
    lines = {}
    for cell, v in ((ana or {}).get("regime_cells") or {}).items():
        if v["trades"] < 20:
            lines[cell] = [f'<span class="muted">{v["trades"]} 笔 · 未证实(&lt;20)</span>']
        else:
            net_cls = "pos" if v["net"] >= 0 else "neg"
            lines[cell] = [f'{v["trades"]} 笔 · 胜 {v["win_rate"]}%',
                           f'<span class="{net_cls}">{v["net"]:+g} 点</span> · PF {v["pf"] if v["pf"] is not None else "∞"}']
    return lines


def _recon_regime_lines(recon) -> dict:
    """对账八格显示行(v2.5): 每格 实盘/回测 各自笔数+胜率 — 同格对照"回测的赢法实盘还成立吗"。
    对账窗口本来就短(样本小), 不做<20灰显压制, 由页面总注"样本少=未证实"兜底"""
    lines = {}
    for cell, v in ((recon or {}).get("regime_recon") or {}).items():
        def _ln(tag, n, w):
            if not n:
                return f'<span class="muted">{tag} 0 笔</span>'
            return f'{tag} {n} 笔 · 胜 {round(w / n * 100)}%'
        lines[cell] = [_ln("实", v["act_n"], v["act_w"]), _ln("回", v["bt_n"], v["bt_w"])]
    return lines


@bp.get("/analysis")
def analysis():
    """策略分析: 关2对账(输入策略id → 回测 vs 实盘 match%); v1.4 更多归因维度待建"""
    sid = request.args.get("strategy_id", type=int)
    a_symbol = request.args.get("symbol") or None   # 归因看哪个品种的回测(默认主品种)
    a_account = request.args.get("account", type=int)  # 看哪个账户的对账(缺省=主账户)
    recon, ana = None, None
    if sid:
        try:
            recon = api.get(f"/reconcile/{sid}",     # 对账恒用主品种(实盘只在主品种交易)
                            **({"account": a_account} if a_account else {}))
        except api.ApiError as e:
            flash(f"对账失败: {e}", "error")
        try:
            ana = api.get(f"/analysis/{sid}", **({"symbol": a_symbol} if a_symbol else {}))
        except api.ApiError as e:
            flash(f"分析失败: {e}", "error")
    return render_template("strategy_analysis.html", recon=recon, ana=ana, sid=sid,
                           regime_lines=_regime_lines(ana),
                           regime_fills=_cells_fills((ana or {}).get("regime_cells")),
                           act_regime_lines=_regime_lines((ana or {}).get("actual")),
                           act_regime_fills=_cells_fills(((ana or {}).get("actual") or {}).get("regime_cells")),
                           recon_regime_lines=_recon_regime_lines(recon))


def _cells_fills(cells) -> dict:
    """八格底色(统一模版): 净点≥0 绿 / <0 红 / 样本<20 灰 — 左色条恒为 regime 本色。
    喂给 m.regime_grid(fills=); 无盈亏数据的格子(占比/对账)不传 = 白底"""
    return {cell: ("#94a3b8" if v["trades"] < 20 else
                   "#16a34a" if v["net"] >= 0 else "#dc2626")
            for cell, v in (cells or {}).items()}


def _matrix_total_lines(data) -> dict:
    """九币矩阵汇总八格显示行: 第1行笔数+胜率(纯计数, 跨品种真实可比);
    第2行净点+PF 灰显(混单位, 金点≠欧点, 只作参考); <20笔=未证实(小样本纪律同口径)"""
    lines = {}
    for cell, v in ((data or {}).get("total_cells") or {}).items():
        if v["trades"] < 20:
            lines[cell] = [f'<span class="muted">{v["trades"]} 笔 · 未证实(&lt;20)</span>']
        else:
            lines[cell] = [f'{v["trades"]} 笔 · 胜 {v["win_rate"]}%',
                           f'<span class="muted">{v["net"]:+g} 点 · PF '
                           f'{v["pf"] if v["pf"] is not None else "∞"}</span>']
    return lines


@bp.get("/regime_matrix")
def regime_matrix():
    """Regime 策略分析(九币矩阵): 输入策略id → 顶部汇总八格(全品种同格相加)
    + 每品种一行八格。重跑按钮复用 ai_backtest(点名+cross_symbol), 现算不落库。"""
    sid = request.args.get("strategy_id", type=int)
    show_years = request.args.get("show_years", type=int)  # 展示窗口: 只筛显示不重跑, 空=全部
    data = None
    if sid:
        try:
            # 九品种各自"自愈建时间线+逐笔贴格", 首次载入(时间线现算20年D1)远超默认15s
            data = api.get("/backtest/regime_matrix", strategy_id=sid, timeout=120,
                           **({"show_years": show_years} if show_years else {}))
        except api.ApiError as e:
            flash(f"载入失败: {e}", "error")
    fills = _cells_fills((data or {}).get("total_cells"))
    # 汇总标题只标"近X年"(汇总页不摆起止明细, 品种表每行有区间); 半年=近0.5年
    win_label, show_opts = "", []
    if data and data.get("symbols"):
        f, t = data["symbols"][0]["from_time"], data["symbols"][0]["to_time"]
        days = (datetime.fromisoformat(t) - datetime.fromisoformat(f)).days
        win_label = f"近{round(days / 365, 1):g}年"   # 5.0→近5年, 0.49→近0.5年, 10.0→近10年
        # 展示窗口档位: 只列严格小于回测区间的(等长=「全部」已覆盖)
        show_opts = [y for y in (1, 2, 3, 5, 10, 20) if y * 365 <= days - 30]
    return render_template("regime_matrix.html", data=data, sid=sid, win_label=win_label,
                           show_years=show_years, show_opts=show_opts,
                           total_lines=_matrix_total_lines(data), matrix_fills=fills)


# 九币矩阵 AI 提示词正文(结果 JSON 追加在末尾)。口径与页面注释一字同源:
# 汇总只有笔数/胜率可比、<20笔未证实、规律=格间拉开+跨品种同向
_REGIME_MATRIX_PROMPT = """\
# 任务: 判断一个交易策略的盈亏是否与市场状态(Regime)相关

## 背景(这是在做什么)
我们有一个策略工厂系统: 同一个策略(同一模板+同一套参数)在多个货币对上、统一时间窗口内
做了悲观口径的历史回测(点差/滑点/佣金全算, SL/TP 同 bar 先碰止损)。每笔回测交易按【入场日】
贴上当天该品种的市场状态标签(Regime), 汇总成"八格战绩"——看这个策略在哪种市场性格里赚钱/亏钱。

## Regime 原理(三字母格子, 只描述当天性格, 绝不预测未来)
每个品种每个交易日由三个二值维度组成一个格子(如 AAB):
- 第1位 长趋势: D1 收盘 > SMA200 → A(长期上行), 否则 B(长期下行)
- 第2位 短趋势: D1 收盘 > SMA20 → A(短期上行), 否则 B(短期下行)
- 第3位 波动:   ATR14 > 过去252日 ATR 中位数 → A(高波动), 否则 B(低波动)
无未来函数。八格 = AAA/AAB/ABA/ABB/BAA/BAB/BBA/BBB。

## 结果 JSON 格式(数据附在最后)
- strategy_id/name/template/main_symbol/timeframe/status: 策略身份(main_symbol=原生品种)
- symbols[]: 每品种一行 — from_time/to_time 回测区间, trades 总笔数,
  unlabeled 无标签未计入笔数, cells = 该品种八格:
  cells.XXX = {trades 笔数, win_rate 胜率%, net 净点(该品种单位), pf 盈亏比(null=无亏损=∞)}
- total_cells: 全部品种同格相加的汇总八格(结构同 cells)
- window_consistent: 各品种窗口是否一致(false 则跨品种对比无效)

## 口径警告(分析前必读)
1. 汇总(total_cells)里只有【笔数、胜率】跨品种真实可比(纯计数); 【净点、PF】混单位——
   不同品种一"点"价值不同, XAUUSD 这类大点值品种会主导汇总, 只作参考。
2. 每品种行内(symbols[].cells)四项都真实(同品种同单位)。
3. 单格 <20 笔 = 样本不足, 结论只能是"未证实", 不是"已证明"。
4. 判定"有规律"的标准: 格间胜率/PF 明显拉开, 且同一格在【多数品种上同向】——
   一两个品种撑起来的亮格是噪音/单品种主导, 不算规律。

## 请回答(结论简短, 每条 1-3 句, 用数据支撑, 不确定就写"未证实")
1. 规律: 该策略的盈亏与 regime 有无规律?(格间是否拉开 + 跨品种是否同向)
2. 结构: 盈亏主要由哪些格/哪些品种贡献? 汇总里的亮格是真规律还是单一品种(如金)主导?
3. 最佳象限: 哪个格表现最好? 依据(笔数/胜率/跨品种一致性)可信吗?
4. 结论: 该策略整体是否达标值得保留? 若限定只在某些 regime 交易能否救活? 都不行就直说淘汰。

## 数据
"""


@bp.get("/regime_matrix/prompt.txt")
def regime_matrix_prompt_txt():
    """九币矩阵 AI 提示词(纯文本): 实验说明+regime原理+JSON格式+要回答的问题+结果JSON。
    复制整段粘给任意 AI 用(与 ai/prompt.txt 同模式)"""
    sid = request.args.get("strategy_id", type=int)
    show_years = request.args.get("show_years", type=int)  # 与页面展示同口径
    if not sid:
        return "error: 缺 strategy_id", 400, {"Content-Type": "text/plain; charset=utf-8"}
    try:
        data = api.get("/backtest/regime_matrix", strategy_id=sid, timeout=120,
                       **({"show_years": show_years} if show_years else {}))
    except api.ApiError as e:
        return f"error: {e}", 502, {"Content-Type": "text/plain; charset=utf-8"}
    import json as _json
    txt = _REGIME_MATRIX_PROMPT + _json.dumps(data, ensure_ascii=False, indent=1, default=str)
    return txt, 200, {"Content-Type": "text/plain; charset=utf-8"}


@bp.post("/<int:strategy_id>/set_visibility")
def set_visibility(strategy_id: int):
    """改可见性(私有/公开/共享) — 打标动作, 低频, 普通提交+flash"""
    try:
        r = api.post(f"/strategies/{strategy_id}/visibility",
                     {"visibility": request.form.get("visibility", "")})
        zh = {"private": "私有", "public": "公开", "shared": "共享"}
        flash(f"#{strategy_id} 可见性 → {zh.get(r['visibility'], r['visibility'])}", "ok")
    except api.ApiError as e:
        flash(f"改可见性失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))


@bp.get("/<int:strategy_id>/trail_prompt")
def trail_prompt(strategy_id: int):
    """AJAX: 插件调优提示词(只调trail不动策略参数, AI出N组→走既有收货/回测/家族对比管道)"""
    try:
        return api.get(f"/strategies/{strategy_id}/trail_prompt",
                       count=request.args.get("count", 20, type=int))
    except api.ApiError as e:
        return {"error": str(e)}, 502


@bp.post("/<int:strategy_id>/trail_batch")
def trail_batch(strategy_id: int):
    """AJAX: 第4步·插件调优批跑(与生成策略分开) — N版内存回测较慢, 超时放宽到300s"""
    payload = request.get_json(silent=True) or {}
    try:
        return api.post(f"/strategies/{strategy_id}/trail_batch",
                        {"trails": payload.get("trails") or []}, timeout=300)
    except api.ApiError as e:
        return {"error": str(e)}, 502


@bp.get("/<int:strategy_id>/trail_compare")
def trail_compare(strategy_id: int):
    """AJAX: 移动止损四档对比(api 内存现算, 不落库) — 透传;
    variant=某档附逐笔明细; gap/start/k=手填参数(调试试数值, 优先于探针)"""
    params = {kk: v for kk, v in request.args.items()
              if kk in ("variant", "gap", "start", "k") and v}
    try:
        return api.get(f"/strategies/{strategy_id}/trail_compare", **params)
    except api.ApiError as e:
        return {"error": str(e)}, 502


@bp.post("/<int:strategy_id>/set_trail")
def set_trail(strategy_id: int):
    """AJAX: 把某档移动止损写进策略 params.trail(空=清除, 回落全局默认)"""
    import json as _json
    raw = request.form.get("trail", "").strip()
    try:
        trail = _json.loads(raw) if raw else None
        r = api.post(f"/strategies/{strategy_id}/trail", {"trail": trail})
        return {"ok": True, "trail": r["trail"]}
    except ValueError:
        return {"error": "trail JSON 格式错误"}, 400
    except api.ApiError as e:
        return {"error": str(e)}, 502


@bp.post("/<int:strategy_id>/basis")
def set_basis(strategy_id: int):
    """编辑备注(basis) — AJAX 失焦即存, 回 JSON; 当前版本唯一可编辑的注释"""
    try:
        r = api.post(f"/strategies/{strategy_id}/basis",
                     {"basis": request.form.get("basis", "")})
        return {"id": r["id"], "basis": r["basis"]}
    except api.ApiError as e:
        return {"error": str(e)}, 400


@bp.get("/market")
def market():
    """策略市场(v5.4 雏形, 只读): public/shared 策略的成绩摘要+实盘汇总。
    红线现在就练: shared 不显示参数, 连 name 都不显示(策略名里嵌着参数)。"""
    rows = []
    try:
        # market=1: 明确的跨用户视图(v5.6 通电后 /backtest/top 默认按当前用户过滤)
        rows = api.get("/backtest/top", visibility="public,shared", market=1, limit=200)["results"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("market.html", rows=rows)


@bp.post("/<int:strategy_id>/mount")
def set_mount(strategy_id: int):
    """挂载到某台 worker / 改某挂载点手数(host_id + 可选 volume; 选完即提交)。
    AJAX(X-Requested-With: fetch)= 回 JSON 由前端原地重取挂载格; 普通提交 = flash+跳回"""
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    raw = request.form.get("volume", "").strip()
    try:
        payload = {"host_id": int(request.form["host_id"])}
        if raw:
            payload["volume"] = float(raw)
        r = api.post(f"/strategies/{strategy_id}/mounts", payload)
        if is_fetch:
            return {"ok": True}
        flash(f"#{strategy_id} 挂载 @ {r['host']} · 手数 "
              f"{('%g' % r['volume']) if r.get('volume') is not None else '默认'}"
              " — runner 下一轮生效", "ok")
    except (ValueError, KeyError):
        if is_fetch:
            return {"error": "host_id/手数格式错误"}, 400
        flash("host_id/手数格式错误", "error")
    except api.ApiError as e:
        if is_fetch:
            return {"error": str(e)}, 502
        flash(f"挂载失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))


@bp.post("/<int:strategy_id>/unmount")
def unmount(strategy_id: int):
    """卸载某挂载点(软停用, 保留手数记忆; 全卸=该策略停跑但状态不变)"""
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    try:
        r = api.delete(f"/strategies/{strategy_id}/mounts/{int(request.form['host_id'])}")
        if is_fetch:
            return {"ok": True, "remaining": r["remaining"]}
        if r["remaining"]:
            flash(f"#{strategy_id} 已卸载该机, 其余 {r['remaining']} 个挂载点继续跑", "ok")
        else:
            flash(f"#{strategy_id} 已无任何挂载 — 停跑(状态不变); 重新挂载或状态切走再切回可恢复",
                  "error")
    except (ValueError, KeyError):
        if is_fetch:
            return {"error": "host_id 格式错误"}, 400
        flash("host_id 格式错误", "error")
    except api.ApiError as e:
        if is_fetch:
            return {"error": str(e)}, 502
        flash(f"卸载失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))


@bp.get("/<int:strategy_id>/mount_cell")
def mount_cell(strategy_id: int):
    """AJAX 片段: 只渲染一个策略的挂载格(挂载操作/状态切换后原地刷新, 不整页重载)"""
    status = (request.args.get("status") or "").upper()
    mv, volume_presets = {"rows": [], "addable": []}, []
    try:
        volume_presets = api.get("/config")["config"].get("volume_presets") or []
        rows_m = [x for x in api.get("/strategies/mounts", ids=str(strategy_id))["mounts"]
                  .get(str(strategy_id), []) if x["enabled"]]
        role = status.lower()
        hosts = [h for h in api.get("/hosts")["hosts"] if h.get("enabled") and h.get("runner")]
        used = {x["host_id"] for x in rows_m}
        mv = {"rows": rows_m,
              "addable": ([h for h in hosts if h["runner"] == role and h["id"] not in used]
                          if role in ("demo", "live") else [])}
    except api.ApiError:
        pass
    return render_template("_mount_cell.html", mc_sid=strategy_id, mc_status=status,
                           mc_mv=mv, volume_presets=volume_presets)


@bp.post("/heal_points")
def heal_points():
    """point 漂移一键治愈(v0.7): 按原始价格×当前point 重算该品种全部 net_points(幂等)"""
    try:
        r = api.post("/trades/heal_points", {"symbol": request.form["symbol"]})
        flash(f"{r['symbol']} 已按当前 point({'%g' % r['point']}) 重算 {r['updated']} 笔 net_points"
              " — 刷新即可看到红条消失", "ok")
    except KeyError:
        flash("缺少品种参数", "error")
    except api.ApiError as e:
        flash(f"治愈失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.analysis"))


@bp.get("/reconcile_stats")
def reconcile_stats():
    """对账统计: 全部有实盘成交的策略, 批量看 回测vs实盘 匹配率(顶部三卡 + 逐策略行)。
    行数据 = api /reconcile/summary(纯读已存结果); 重算 = 页面循环调 reconcile_one。"""
    rows = []
    try:
        rows = api.get("/reconcile/summary")["strategies"]
    except api.ApiError as e:
        flash(f"读取对账统计失败: {e}", "error")
    return render_template("reconcile_stats.html", rows=rows)


@bp.post("/<int:strategy_id>/reconcile")
def reconcile_one(strategy_id: int):
    """AJAX: 重算单个策略对账(全部账户整组算), 返回 accounts 列表按账户回填各行"""
    try:
        r = api.get(f"/reconcile/{strategy_id}")
    except api.ApiError as e:
        return {"error": str(e)}, 502
    return {"id": strategy_id, "accounts": r.get("accounts") or []}


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
    return render_template("_attribution_body.html", ana=ana,
                           regime_lines=_regime_lines(ana),
                           regime_fills=_cells_fills((ana or {}).get("regime_cells")))


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
        wd = request.form.get("window_days", type=int)   # 九币矩阵页: 5/10/15/20年统一窗口
        if wd:
            payload["window_days"] = wd
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
            # 批次标签 → basis(生因): 事后排名页搜"标签"按批查找/分组统计(验尺实验用)
            "label": request.form.get("label", "").strip() or None,
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
