"""策略组页面: 列表(index) / 生成+MQ5转化(generate_page) / 分析(analysis, 骨架) / 状态流转
UI 拆分(2026-07-13): 生成=进货(偶发), 列表=日常主战场, 各自成页; 导航挂「策略▾」下拉。"""
from datetime import datetime

from flask import (Blueprint, flash, jsonify, redirect, render_template, request,
                   session, url_for)

import api_client as api

bp = Blueprint("strategies", __name__, url_prefix="/strategies")

TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"]

# 薄样本提醒(样本层事实, 三处八格共用一份措辞): 数字照给, 只提示巧合风险
BADGE = '<span class="neg" title="n&lt;20 笔: 数字如实, 但样本太小 — 单笔运气就能翻转胜率/PF, 可能只是巧合; 攒够笔数再下结论">！注意: 样本不足</span>'


@bp.get("/")
def index():
    """策略列表排名(唯一工作台): 全部策略(含未回测, 成绩为空沉底) + 成绩/评分/健壮性
    + 筛选(品种/券商/状态/多条件)/搜索/排名参数模板。数据走 /backtest/top(LEFT JOIN 版)。"""
    a = request.args
    template = a.get("template") or None
    symbol = a.get("symbol") or None
    broker = a.get("broker") or None
    status = a.get("status") or None
    archived = a.get("archived") or ""   # 归档三态: ""=不显示(默认)/with=一起显示/only=只看归档
    visibility = a.get("visibility") or None
    q_field = a.get("q_field") or "id"   # 默认搜索字段=策略ID(2026-08-02 Frank 定)
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
    mounts_view = {}     # 挂载列(纯显示): {sid: {rows: [启用挂载]}}
    hosts_runner = []    # 调度下拉的机器清单(有运行角色的启用主机)
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
                     ("broker", broker), ("status", status), ("visibility", visibility),
                     ("archived", archived)):
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
            # 挂载列=纯显示且只显示【生效中】的挂载(2026-08-02 Frank 修bug):
            # 生效 = enabled 且 机器角色==当前状态(两把钥匙同判) — 跨池残留记忆行
            # 不显示(留库等 v7.4 归置), 否则看着像双跑
            for r in results:
                role = (r.get("status") or "").lower()
                mounts_view[str(r["strategy_id"])] = {
                    "rows": [x for x in mnt.get(str(r["strategy_id"]), [])
                             if x["enabled"] and x["runner"] == role]}
        # 调度下拉的机器清单(有运行角色的启用主机)
        hosts_runner = [h for h in api.get("/hosts")["hosts"]
                        if h.get("enabled") and h.get("runner")]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    total_pages = max((total + page_size - 1) // page_size, 1)  # 向上取整
    base_args = {k: v for k, v in a.items() if k != "page"}     # 翻页链接保留其它筛选
    return render_template("strategies.html", results=results, volume_presets=volume_presets,
                           volume_default=volume_default, mounts_view=mounts_view,
                           hosts_runner=hosts_runner,
                           symbol=symbol, broker=broker, min_actual_trades=min_actual_trades,
                           status=status, archived=archived, visibility=visibility,
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

    小样本待遇(2026-08-06 与 Frank 定"不能不显示"): 数字一律照给(哪怕 1 笔)、底色照按盈亏上,
    <20 笔只在旁边加一句「！注意: 样本不足」提醒 — 看得清是第一位, 风险靠提示而不是靠藏数字。
    用词分层: "样本不足"=样本层的事实(单笔运气就能翻转, 可能是巧合);
    "未证实"=结论层的判词(留给文字结论/文档, 不当格子角标用)。
    """
    lines = {}
    for cell, v in ((ana or {}).get("regime_cells") or {}).items():
        net_cls = "pos" if v["net"] >= 0 else "neg"
        head = f'{v["trades"]} 笔 · 胜 {v["win_rate"]}%'
        if v["trades"] < 20:
            head += ' ' + BADGE
        lines[cell] = [head,
                       f'<span class="{net_cls}">{v["net"]:+g} 点</span> · PF {v["pf"] if v["pf"] is not None else "∞"}']
    return lines


def _recon_regime_lines(recon) -> dict:
    """对账八格显示行(v2.5): 每格 实盘/回测 各自笔数+胜率+净点 — 同格对照"回测的赢法实盘还成立吗"。
    净点(2026-08-06 Frank 要): 两行各自带净点, 正绿负红(与全站盈亏定色同款);
    两边口径不同(实盘含真实点差滑点, 回测悲观撮合)故只并排不做减法 — 偏差看页面上方"盈亏精度"。
    对账窗口本来就短(样本小), 不做压制, 由页面总注兜底"""
    lines = {}
    for cell, v in ((recon or {}).get("regime_recon") or {}).items():
        def _ln(tag, n, w, net):
            if not n:
                return f'<span class="muted">{tag} 0 笔</span>'
            cls = "pos" if net >= 0 else "neg"
            return (f'{tag} {n} 笔 · 胜 {round(w / n * 100)}% · '
                    f'<span class="{cls}">{net:+g} 点</span>')
        lines[cell] = [_ln("实", v["act_n"], v["act_w"], v.get("act_net") or 0),
                       _ln("回", v["bt_n"], v["bt_w"], v.get("bt_net") or 0)]
    return lines


def _recon_fills(recon) -> dict:
    """对账八格底色: 按【实盘】净点方向(真金是真相; 悬停注明色跟实盘) — 与其他八格同一套
    绿赚红亏; 一格里两个净点, 只能有一个染色源, 选实盘"""
    return {cell: ("#16a34a" if (v.get("act_net") or 0) >= 0 else "#dc2626")
            for cell, v in ((recon or {}).get("regime_recon") or {}).items()
            if v.get("act_n")}   # 实盘该格没有成交 = 不染色(白底)


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
                           recon_regime_lines=_recon_regime_lines(recon),
                           recon_fills=_recon_fills(recon))


def _cells_fills(cells) -> dict:
    """八格底色(统一模版): 净点≥0 绿 / <0 红 — 左色条恒为 regime 本色。
    2026-08-06 Frank 定"看得清第一位": 薄样本不再灰显压制(数字和颜色都照给),
    风险由格内「！注意: 样本不足」提醒。无盈亏数据的格子(占比/对账)不传 = 白底"""
    return {cell: ("#16a34a" if v["net"] >= 0 else "#dc2626")
            for cell, v in (cells or {}).items()}


def _matrix_total_lines(data) -> dict:
    """九币矩阵汇总八格显示行: 第1行笔数+胜率(纯计数, 跨品种真实可比);
    第2行净点+PF 灰显(混单位, 金点≠欧点, 只作参考); <20笔=样本不足(小样本纪律同口径)"""
    lines = {}
    for cell, v in ((data or {}).get("total_cells") or {}).items():
        head = f'{v["trades"]} 笔 · 胜 {v["win_rate"]}%'
        if v["trades"] < 20:
            head += ' ' + BADGE
        lines[cell] = [head,
                       f'<span class="muted">{v["net"]:+g} 点 · PF '
                       f'{v["pf"] if v["pf"] is not None else "∞"}</span>']
    return lines


@bp.get("/regime_matrix")
def regime_matrix():
    """Regime 策略分析(九币矩阵): 输入策略id → 顶部汇总八格(全品种同格相加)
    + 每品种一行八格。重跑按钮复用 ai_backtest(点名+cross_symbol), 现算不落库。"""
    sid = request.args.get("strategy_id", type=int)
    show_years = request.args.get("show_years", type=int)  # 展示窗口: 只筛显示不重跑, 空=全部
    regime_version = request.args.get("regime_version", type=int)  # 空=当前默认版本
    sym_filter = (request.args.get("symbol") or "").strip().upper() or None  # 品种过滤: 空=全部
    data = None
    rv = {"current": None, "versions": []}
    if sid:
        try:
            rv = api.get("/regime/versions")   # 版本下拉(v0.2): 同一批trades换版本看归因
            # 九品种各自"自愈建时间线+逐笔贴格", 首次载入(时间线现算20年D1)远超默认15s
            data = api.get("/backtest/regime_matrix", strategy_id=sid, timeout=120,
                           **({"show_years": show_years} if show_years else {}),
                           **({"regime_version": regime_version} if regime_version else {}))
        except api.ApiError as e:
            flash(f"载入失败: {e}", "error")
    # 品种过滤(纯展示层): 选单品种 → 汇总八格=它自己的八格(同单位四项全真), 品种表只剩一行
    syms_all = [s["symbol"] for s in (data or {}).get("symbols") or []]
    if data and sym_filter and sym_filter in syms_all:
        only = next(s for s in data["symbols"] if s["symbol"] == sym_filter)
        data["symbols"] = [only]
        data["total_cells"] = only["cells"]
        data["total_trades"] = only["trades"]
        data["total_unlabeled"] = only["unlabeled"]
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
                           regime_version=regime_version,
                           rv_current=rv["current"], rv_versions=rv["versions"],
                           sym_filter=sym_filter, syms_all=syms_all,
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
  unlabeled 无标签未计入笔数, cells = 该品种【全区间】八格:
  cells.XXX = {trades 笔数, win_rate 胜率%, net 净点(该品种单位), pf 盈亏比(null=无亏损=∞)}
- symbols[].sweep: 该品种各【展示窗口】切片八格 — {"近1年": cells, "近2年": ..., "近5年"...}
  (窗口=从回测终点往回数 N 年, 只列严格短于回测区间的; 全区间即 cells, 不重复)
- total_cells / total_sweep: 全部品种同格相加的汇总(结构同上)
- window_consistent: 各品种窗口是否一致(false 则跨品种对比无效)

## 口径警告(分析前必读)
1. 汇总(total_*)里只有【笔数、胜率】跨品种真实可比(纯计数); 【净点、PF】混单位——
   不同品种一"点"价值不同, XAUUSD 这类大点值品种会主导汇总, 只作参考。
2. 每品种行内(symbols[].cells / .sweep)四项都真实(同品种同单位)。
3. 单格 <40 笔 = 样本薄, <20 笔 = 未证实 — 这类判断写在文字结论里用数字说明,
   表格内不加任何特殊符号或标注(不用 ⚠/括号注记, 笔数本身已展示, 让数字自己说话)。
4. 判定"有规律"的双判据(缺一不可):
   - 时间稳健: 同一格在【≥2/3 的窗口切片】同向(排除单窗口运气);
   - 跨品种一致: 同一格在【≥2/3 的品种】同向(排除单品种主导)。
5. 掩码归纳: 若多个达标格共享两位字母(如 ABA+BBA 共享 ?BA=短↓+高波), 归纳为掩码报告。

## 输出格式(严格按此模板, 表格用 markdown, 全程不用特殊符号/emoji)

### 一、前三 Regime(按跨窗口稳定性排序, 汇总口径)
| 展示窗口 | ① XXX | ② XXX | ③ XXX |
每行一个窗口(近1年→全区间), 格内"N笔 · 胜x.x% · 净点 · PF x.xx"(四值, 笔数在前);
末行【稳定性】= 各格"正窗口数/总窗口数"; 表后一行【掩码归纳】。

### 二、建议货币对 Top3(该策略该在哪个品种上用)
| # | 品种 | 依据(前三格在该品种的表现) | 主货币 |
依据写"格 X/Y 窗口正 + PF 范围"; 主货币列: 该行品种 = main_symbol 时写 match, 否则留空。
表后【淘汰提示】: 前三格全窗口负的品种点名勿碰。

### 三、一句话结论
掩码/格 + 双判据数字 + 建议(主用/备选/淘汰); 样本薄(<40笔)、盈利薄(PF<1.1)之类
的保留意见用平实文字带数字写出, 不确定就写"未证实"。

## 数据
"""


_CELLS8 = ("AAA", "AAB", "ABA", "ABB", "BAA", "BAB", "BBA", "BBB")


@bp.get("/tree")
def strategy_tree():
    """策略谱系(只读): 模板 → 参数实例(平铺, AI 出身=basis 灰字) → 门变体挂父下。
    归档不显示(标题计数); 操作(状态/挂载/克隆)回各自页面 — 一页一职"""
    template = request.args.get("template") or None
    symbol = (request.args.get("symbol") or "").strip().upper() or None
    timeframe = request.args.get("timeframe") or None
    sid = request.args.get("strategy_id", type=int)   # 直查: 输id显示该策略家族(门id自动定位到父)
    templates, symbols, data = [], [], None
    try:
        templates = sorted(api.get("/strategies/templates")["templates"])
        symbols = [s["symbol"] for s in api.get("/symbols")["symbols"]]
        if sid:
            data = api.get("/strategies/tree", strategy_id=sid)
            template, symbol = data["template"], data["symbol"]
        elif template and symbol:
            data = api.get("/strategies/tree", template=template, symbol=symbol,
                           **({"timeframe": timeframe} if timeframe else {}))
        if data:   # 后处理对两种入口一视同仁(曾因缩进只跑浏览分支 → 直查什么都看不见)
            import json as _json

            def _win(n):   # 回测窗标签: 近X年(悬停看起止) — 窗口不同的成绩不可互比
                if not n.get("bt_from") or not n.get("bt_to"):
                    return   # api 未带窗口字段(旧版/未回测) → 显示 —, 不炸
                days = (datetime.fromisoformat(n["bt_to"])
                        - datetime.fromisoformat(n["bt_from"])).days
                n["win"] = f"近{round(days / 365, 1):g}年" if days >= 350 else f"{days}天"
                n["win_title"] = f"{n['bt_from'][:10]} ~ {n['bt_to'][:10]}"
            gate_count = 0
            for n in data["instances"]:   # 摘要在视图层拼好, 模板只管摆
                _win(n)
                n["summary"] = _gate_summary(n["gate"]) if n.get("gate") \
                    else _param_summary(n["params"])
                gate_count += 1 if n.get("gate") else 0
                # 悬停/展开 = metadata 原文(regime 只是其中一键, 将来 trail 等自动跟显)
                if n.get("gate"):
                    n["hover"] = _json.dumps(n.get("metadata") or {}, ensure_ascii=False)
                for g in n["gates"]:
                    _win(g)
                    g["summary"] = _gate_summary(g["gate"])
                    g["hover"] = _json.dumps(g.get("metadata") or {}, ensure_ascii=False)
                    gate_count += 1
            data["gate_count"] = gate_count
    except api.ApiError as e:
        flash(f"载入失败: {e}", "error")
    return render_template("strategy_tree.html", templates=templates, symbols=symbols,
                           template=template, symbol=symbol, timeframe=timeframe,
                           sid=sid, tfs=TIMEFRAMES, data=data)


def _param_summary(params) -> str:
    """实例行参数摘要: k+v 紧凑串(悬停看完整名字)"""
    return " · ".join(f"{k}{v}" for k, v in sorted((params or {}).items()))


def _gate_summary(gate) -> str:
    """门变体摘要: v1: ABA×1 · BBA×0.5"""
    return f"v{gate['version']}: " + " · ".join(
        f"{c}×{float(mv):g}" for c, mv in sorted(gate["cells"].items()))


@bp.post("/<int:strategy_id>/clone_gate")
def clone_gate(strategy_id: int):
    """克隆带门(v0.3): 矩阵页勾格+倍率+版本 → api 收货管道(校验/判重/谱系) → 新实例 id"""
    cells = {c: float(request.form.get(f"m_{c}") or 1)
             for c in _CELLS8 if request.form.get(f"g_{c}")}
    try:
        r = api.post(f"/strategies/{strategy_id}/clone_gate",
                     {"version": int(request.form.get("version") or 0), "cells": cells})
        if r.get("created"):
            flash(f"克隆成功: #{r['id']}(parent=#{strategy_id}, 带门) — "
                  f"上方输入 {r['id']} 载入并跑回测, 引擎将带门真跑", "ok")
        elif r.get("existing_id"):
            flash(f"同门实例已存在: #{r['existing_id']}({r.get('existing_status')}) — 直接用它", "ok")
        else:
            flash(f"未创建: {r.get('error') or r}", "error")
    except (api.ApiError, ValueError) as e:
        flash(f"克隆失败: {e}", "error")
    return redirect(url_for("strategies.regime_matrix", strategy_id=strategy_id))


@bp.get("/regime_matrix/prompt.txt")
def regime_matrix_prompt_txt():
    """九币矩阵 AI 提示词(纯文本): 实验说明+regime原理+JSON格式+要回答的问题+结果JSON。
    复制整段粘给任意 AI 用(与 ai/prompt.txt 同模式)"""
    sid = request.args.get("strategy_id", type=int)
    if not sid:
        return "error: 缺 strategy_id", 400, {"Content-Type": "text/plain; charset=utf-8"}
    regime_version = request.args.get("regime_version", type=int)  # 与页面版本下拉同口径
    try:
        # sweep=全维度(全窗口×全品种切片): AI 做时间稳健+跨品种双判据, 与页面当前展示窗口无关
        data = api.get("/backtest/regime_matrix", strategy_id=sid, timeout=120, sweep=1,
                       **({"regime_version": regime_version} if regime_version else {}))
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


# 挂载/卸载的 web 表单路由已退役(2026-08-02 Frank 定"前面只显示, 后面切换"):
# 挂载列=纯显示, 切换唯一入口=操作列状态下拉(set_status 自动挂池/清挂载);
# api 端点(/strategies/{id}/mounts)保留给程序化/未来 pool UI。


@bp.get("/<int:strategy_id>/mount_cell")
def mount_cell(strategy_id: int):
    """AJAX 片段: 只渲染一个策略的挂载格(状态切换后原地刷新, 不整页重载; 纯显示)"""
    status = (request.args.get("status") or "").upper()
    mv = {"rows": []}
    try:
        # 只显示生效中的挂载(enabled 且 角色==状态), 与列表页同判 — 残留记忆行不显示
        mv = {"rows": [x for x in api.get("/strategies/mounts", ids=str(strategy_id))
                       ["mounts"].get(str(strategy_id), [])
                       if x["enabled"] and x["runner"] == status.lower()]}
    except api.ApiError:
        pass
    return render_template("_mount_cell.html", mc_sid=strategy_id, mc_status=status, mc_mv=mv)


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
    rows, recon_hours, recon_last = [], 24, None
    try:
        rows = api.get("/reconcile/summary")["strategies"]
        cfg = api.get("/config")["config"]
        recon_hours = int(cfg.get("recon_hours") or 0)   # 自动对账频率(小时, 0=关)
        recon_last = cfg.get("recon_last_run")
    except (api.ApiError, TypeError, ValueError) as e:
        flash(f"读取对账统计失败: {e}", "error")
    return render_template("reconcile_stats.html", rows=rows,
                           recon_hours=recon_hours, recon_last=recon_last)


@bp.get("/reconcile/cards")
def reconcile_cards():
    """对账三卡(AJAX): 透传筛选给 api, 算法唯一在 api._recon_cards — 页面不算数"""
    try:
        params = {k: v for k, v in request.args.items() if v not in ("", None)}
        return jsonify(api.get("/reconcile/summary", **params)["cards"])
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.post("/reconcile/hours")
def save_recon_hours():
    """保存自动对账频率(config recon_hours, 仅 admin): 距上次满 N 小时就跑一遍, 0=关闭"""
    if session.get("dev_user_id") != 1:
        flash("此项仅管理员(admin)可改 — 其他用户只读", "error")
        return redirect(url_for("strategies.reconcile_stats"))
    try:
        h = int(request.form.get("recon_hours", 24))
        api.put("/config/recon_hours", {"value": h})
        flash(f"自动对账频率已保存: {'关闭' if h == 0 else f'每 {h} 小时'}"
              " — 心跳主节点下一拍(30秒内)按新频率走", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("strategies.reconcile_stats"))


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


@bp.get("/<int:strategy_id>/profile.json")
def profile_json(strategy_id: int):
    """策略 Profile(v0.7): 结论级画像 JSON(读时现拼) — 人看/贴给AI/未来预测模块引用"""
    try:
        return api.get(f"/strategies/{strategy_id}/profile")
    except api.ApiError as e:
        return {"error": str(e)}, 502


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
            # 批次标签 → basis(生因): 事后排名页/Regime筛选页按标签整批圈。
            # 时间戳无论如何都追加(2026-08-03 Frank 定): 有文本用文本没有用"优化策略",
            # 每批唯一 → 筛选圈批不串批
            "label": (request.form.get("label", "").strip() or "优化策略")
                     + datetime.now().strftime("%y%m%d-%H%M%S"),
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
    """单策略回测 (成本用系统默认; 结果在回测页排名可见)。
    AJAX 提交(data-post-ajax)回 JSON 就地回显, 不刷整页; 普通提交仍走 flash+重定向"""
    ajax = request.headers.get("X-Requested-With") == "fetch"
    try:
        api.post("/backtest/run", {"strategy_ids": [strategy_id]})
        if ajax:
            return {"ok": True, "message": "已投队列"}
        flash(f"策略 #{strategy_id} 回测已启动, 结果见回测页", "ok")
    except api.ApiError as e:
        if ajax:
            return {"error": str(e)}, 502
        flash(f"回测启动失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))


# 批量删除归档的 web 表单路由已退役(2026-08-02 Frank 定"先不用了"):
# 单个删除走操作列调度下拉(死因记 manual); api /strategies/archive 端点保留(死因码在那边),
# 将来要批量按需恢复 UI。


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


@bp.post("/<int:strategy_id>/dispatch")
def dispatch(strategy_id: int):
    """调度下拉(2026-08-02 Frank 定): target = host:<id>(挂到该机, 状态自动跟机器角色)
    或 CANDIDATE/ARCHIVED(状态切换)。响应带 status, JS 原地更新徽章+挂载格。"""
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    t = request.form.get("target", "")
    try:
        if t.startswith("host:"):
            r = api.post(f"/strategies/{strategy_id}/mounts", {"host_id": int(t[5:])})
            result = {"status": r.get("status"), "host": r.get("host")}
            msg = f"#{strategy_id} 已挂到 {r.get('host')}({(r.get('status') or '').lower()}) — runner 下一轮生效"
        elif t in ("CANDIDATE", "ARCHIVED"):
            result = api.post(f"/strategies/{strategy_id}/status", {"status": t})
            msg = f"#{strategy_id} → {'空闲(停跑, 已清挂载)' if t == 'CANDIDATE' else '删除归档'}"
        else:
            raise ValueError(f"未知调度目标 {t!r}")
        if is_fetch:
            return result
        flash(msg, "ok")
    except (api.ApiError, KeyError, ValueError) as e:
        if is_fetch:
            return {"error": str(e)}, 400
        flash(f"调度失败: {e}", "error")
    return redirect(request.referrer or url_for("strategies.index"))
