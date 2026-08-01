"""品种页: 品种主档(唯一数据源)的独立维护界面

品种一切信息只在 symbols 表: 登记(向券商校验)、精度、下载开关、每品种起始日期。
下载/回测/策略生成都从这里读。本页是它唯一的管理入口。
"""
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import api_client as api

bp = Blueprint("symbols", __name__, url_prefix="/symbols")


@bp.get("/")
def index():
    """配置·货币对: 品种主档(登记/列表) — 精度/下载/清空都在「下载」页"""
    symbols = []
    try:
        symbols = api.get("/symbols")["symbols"]
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("symbols.html", symbols=symbols)


@bp.get("/backtest")
def backtest_params():
    """配置·策略参数: 生成收货上限 + 成本模型 + 回测单批上限 + OOS 切分"""
    costs, batch_limit, oos_split, mt5_days = {}, 500, 0.7, [7, 30, 90]
    window_days = None
    runtime_write, runtime_gap, gate, recon_tol = 5, 15, {}, 2
    generate_limit, worker_params, regime_params = 500, {}, {}
    regime_versions, regime_current = [], None
    auto_sync_hours = None
    download_timeframes = []  # 唯一源=config表(schema/049种子); api不可用即空
    volume_presets = []  # 唯一源=config表(schema/030种子); api不可用即空(铁律欠账4)
    volume_default = None
    try:
        cfg = api.get("/config")["config"]
        costs = cfg.get("backtest_costs", {})
        batch_limit = cfg.get("backtest_batch_limit", 500)
        generate_limit = cfg.get("generate_batch_limit", 500)
        volume_presets = cfg.get("volume_presets") or []
        volume_default = cfg.get("volume_default")
        oos_split = cfg.get("backtest_oos_split", 0.7)
        window_days = cfg.get("backtest_window_days")
        mt5_days = cfg.get("mt5_trades_days") or [7, 30, 90]
        runtime_write = cfg.get("runtime_write_minutes", 5)
        runtime_gap = cfg.get("runtime_gap_minutes", 15)
        gate = cfg.get("cross_symbol_gate") or {}
        recon_tol = cfg.get("recon_pair_tol_minutes", 2)
        worker_params = cfg.get("worker_params") or {}
        download_timeframes = cfg.get("download_timeframes") or []
        auto_sync_hours = cfg.get("auto_sync_hours")   # 只读展示(schema/055, 管理员库改)
        # Regime 口径版本化(v0.2): 唯一源 = regime_versions 表, 下拉选当前默认
        rv = api.get("/regime/versions")
        regime_versions = rv["versions"]
        regime_current = rv["current"]
        regime_params = next((v["params"] for v in regime_versions
                              if v["id"] == regime_current), {})
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("config_backtest.html", costs=costs, batch_limit=batch_limit,
                           window_days=window_days,
                           generate_limit=generate_limit, volume_presets=volume_presets,
                           volume_default=volume_default,
                           oos_split=oos_split, mt5_days=mt5_days,
                           runtime_write=runtime_write, runtime_gap=runtime_gap, gate=gate,
                           recon_tol=recon_tol, worker_params=worker_params,
                           regime_params=regime_params,
                           regime_versions=regime_versions, regime_current=regime_current,
                           auto_sync_hours=auto_sync_hours,
                           download_timeframes=download_timeframes)


@bp.post("/config/volume-presets")
def save_volume_presets():
    """保存默认手数+手数预设(config: volume_default / volume_presets); 校验在 api 侧把关"""
    raw = request.form.get("volume_presets", "").replace("，", ",")
    d_raw = request.form.get("volume_default", "").strip()
    try:
        vals = [float(x.strip()) for x in raw.split(",") if x.strip()]
        api.put("/config/volume_presets", {"value": vals})
        if d_raw:
            api.put("/config/volume_default", {"value": float(d_raw)})
        flash(f"手数已保存: 默认 {d_raw or '(未改)'} · 预设 {vals}", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/generate-limit")
def save_generate_limit():
    """保存生成单批收货上限(config: generate_batch_limit; 所有生成入口共用, 校验在 api 侧)"""
    try:
        api.put("/config/generate_batch_limit",
                {"value": int(request.form["generate_limit"])})
        flash("生成单批收货上限已保存", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/worker-params")
def save_worker_params():
    """保存 worker 参数(config: worker_params)— 上报节奏/批量, 用户按网络自调;
    下发走 announce 应答, worker 1~2 分钟自动领到, 无需重启"""
    try:
        # PUT 要求键完整: 本表单只管4个上报键, 下载节流2键(下载页管)从现值合并带上
        cur = api.get("/config")["config"].get("worker_params") or {}
        api.put("/config/worker_params", {"value": {**cur, **{
            k: int(request.form[k]) for k in
            ("heartbeat_seconds", "announce_seconds", "bars_batch", "decision_keep_days")}}})
        flash("worker 参数已保存 — 各 worker 下次报到(约1分钟)自动领取生效", "ok")
    except (api.ApiError, ValueError, KeyError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


def _admin_only() -> bool:
    """数据管线两项(下载周期层/自动同步间隔)只有 admin(owner id 1)可改, 其他人只读
    (2026-08-01 Frank 定) — 与 /admin/* 门禁同一判据, 上真登录后一起换真凭据"""
    if session.get("dev_user_id") == 1:
        return True
    flash("此项仅管理员(admin)可改 — 其他用户只读", "error")
    return False


@bp.post("/config/download-timeframes")
def save_download_timeframes():
    """保存下载周期层(config: download_timeframes, 仅 admin) — M1 固定必含(唯一原始数据),
    高周期按需勾选(D1 默认勾, regime 长视野用); 下次点同步按新层派任务"""
    if not _admin_only():
        return redirect(url_for("symbols.backtest_params"))
    try:
        api.put("/config/download_timeframes",
                {"value": ["M1"] + request.form.getlist("tf")})
        flash("下载周期已保存 — 下次触发同步按新周期层派任务", "ok")
    except api.ApiError as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/auto-sync-hours")
def save_auto_sync_hours():
    """保存自动同步间隔(config: auto_sync_hours, 仅 admin) — 心跳主节点下一拍(30秒内)生效"""
    if not _admin_only():
        return redirect(url_for("symbols.backtest_params"))
    try:
        v = int(request.form.get("auto_sync_hours", 6))
        api.put("/config/auto_sync_hours", {"value": v})
        flash(f"自动同步间隔已保存: {'关闭' if v == 0 else f'每 {v} 小时'} — 30 秒内生效, 不用重启", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/regime-params")
def save_regime_params():
    """保存 Regime 口径 → 版本化(v0.2): 新参数生成 v{新id}并设为当前;
    重复参数自动匹配回现有版本(提示"这是vN")。只存不重建 — 重建去 Regime 页显式点。"""
    try:
        # 表单是 下拉(类型)+数字(周期) — 边界显式化; 存储格式仍是 'sma200' 字符串
        r = api.post("/regime/versions", {"params": {
            "long_ma": request.form.get("long_kind", "sma") + request.form.get("long_n", "200"),
            "short_ma": request.form.get("short_kind", "sma") + request.form.get("short_n", "20"),
            "atr_n": int(request.form.get("atr_n", 14)),
            "vol_win": int(request.form.get("vol_win", 252)),
            "vol_q": float(request.form.get("vol_q", 0.5)),
        }})
        if r["created"]:
            flash(f"已生成新版本 v{r['id']} 并设为当前 — 去「数据 → 市场状态 Regime」"
                  f"页对它点重建生成时间线", "ok")
        else:
            flash(f"这套参数已是 v{r['id']} — 已切换为当前版本(时间线沿用, 无需重建)", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/regime-version-select")
def select_regime_version():
    """切换当前默认 Regime 版本(下拉即提交) — 全站读时贴格随之走该版本时间线"""
    try:
        vid = int(request.form.get("version_id", 0))
        r = api.post("/regime/versions/select", {"id": vid})
        p = r["params"]
        flash(f"当前 Regime 版本 → v{vid}"
              f"({p['long_ma']}/{p['short_ma']}/ATR{p['atr_n']}/{p['vol_win']}日/{p['vol_q']})", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"切换失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/regime-params-reset")
def reset_regime_params():
    """Regime 口径一键恢复默认 — 默认值唯一权威在 api(regime.DEFAULT_PARAMS), web 不复制"""
    try:
        r = api.post("/regime/params/reset")
        p = r["params"]
        flash(f"已恢复默认口径: {p['long_ma']}/{p['short_ma']}/ATR{p['atr_n']}"
              f"/{p['vol_win']}日/{p['vol_q']}分位 — 生效需去 Regime 页点重建", "ok")
    except api.ApiError as e:
        flash(f"恢复失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/recon-tol")
def save_recon_tol():
    """保存对账配对容差(config: recon_pair_tol_minutes)— 回测与实盘时间窗口差距"""
    try:
        v = int(request.form.get("recon_pair_tol_minutes", 2))
        api.put("/config/recon_pair_tol_minutes", {"value": v})
        flash(f"对账时间窗口差距已保存: ±{v} 分钟(下次对账生效)", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/cross-gate")
def save_cross_gate():
    """保存交叉测试门槛(config: cross_symbol_gate)。空输入框 = null = 不检查该项;
    胜率输入百分数, 存 0~1 小数"""
    try:
        def num(name, scale=1.0, as_int=False):
            raw = request.form.get(name, "").strip()
            if not raw:
                return None
            v = float(raw) * scale
            return int(v) if as_int else v
        gate = {"min_trades": num("min_trades", as_int=True),
                "min_win_rate": num("min_win_rate", 0.01),
                "min_net_points": num("min_net_points"),
                "min_pf": num("min_pf"),
                "max_dd_points": num("max_dd_points")}
        api.put("/config/cross_symbol_gate", {"value": gate})
        parts = [f"{k}={v}" for k, v in gate.items() if v is not None]
        flash("交叉测试门槛已保存: " + (", ".join(parts) if parts else "全空(不设门槛)"), "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/runtime")
def save_runtime():
    """保存策略运行状态节奏(config: runtime_write_minutes / runtime_gap_minutes)
    约束在这里把关: 两值为正整数, 裂段阈值必须大于写入间隔(否则节流被误判成断线)"""
    try:
        write_min = int(request.form.get("runtime_write_minutes", 5))
        gap_min = int(request.form.get("runtime_gap_minutes", 15))
        if write_min < 1 or gap_min <= write_min:
            flash(f"裂段阈值({gap_min})必须大于写入间隔({write_min}), 且都为正整数", "error")
            return redirect(url_for("symbols.backtest_params"))
        api.put("/config/runtime_write_minutes", {"value": write_min})
        api.put("/config/runtime_gap_minutes", {"value": gap_min})
        flash(f"运行状态节奏已保存: 写入间隔 {write_min} 分钟 / 裂段阈值 {gap_min} 分钟", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.post("/config/mt5-days")
def save_mt5_days():
    """保存 MT5 流水天数预设(config: mt5_trades_days)— 流水页 chips 由它渲染"""
    raw = request.form.get("mt5_trades_days", "").replace("，", ",")
    try:
        days = [int(x.strip()) for x in raw.split(",") if x.strip()]
        api.put("/config/mt5_trades_days", {"value": days})
        flash(f"流水天数预设已保存: {days}", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.backtest_params"))


@bp.get("/ranking")
def ranking():
    """配置·排名参数模板: 四维加权评分模板(增删改)"""
    rank_templates = []
    try:
        rank_templates = api.get("/config")["config"].get("ranking_templates", [])
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("config_ranking.html", rank_templates=rank_templates)


@bp.post("/config/ranks")
def save_ranks():
    """保存排名参数模板(config: ranking_templates)。UI 可增删改:
    删除=勾『删』或清空名称; 新增=填底部空白行; 校验在 api 侧把关"""
    tpls, i = [], 0
    while f"rt_name_{i}" in request.form:
        name = request.form[f"rt_name_{i}"].strip()
        if name and not request.form.get(f"rt_del_{i}"):
            try:
                tpls.append({
                    "name": name,
                    "stable": float(request.form.get(f"rt_stable_{i}") or 0),
                    "profit": float(request.form.get(f"rt_profit_{i}") or 0),
                    "risk": float(request.form.get(f"rt_risk_{i}") or 0),
                    "robust": float(request.form.get(f"rt_robust_{i}") or 0),
                    "min_trades": int(request.form.get(f"rt_mt_{i}") or 0),
                })
            except ValueError:
                flash(f"模板 {name}: 权重/笔数必须是数字", "error")
                return redirect(url_for("symbols.index"))
        i += 1
    try:
        api.put("/config/ranking_templates", {"value": tpls})
        flash(f"排名参数模板已保存({len(tpls)} 个)", "ok")
    except api.ApiError as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("symbols.ranking"))


@bp.post("/add")
def add():
    """登记品种: 异步券商校验(v7.2 单向化) — 下载 worker 领任务查 MT5, 1~2 分钟出结果"""
    try:
        result = api.post("/symbols", {
            "symbol": request.form["symbol"].strip().upper(),
            "data_start": request.form.get("data_start", "2015-01-01").strip(),
        })
        flash(f"{result['symbol']} {result.get('hint', '已登记, 等待校验')}", "ok")
    except (api.ApiError, KeyError) as e:
        flash(f"登记失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/<symbol>/update")
def update(symbol):
    """改下载开关 / 起始日期"""
    try:
        payload = {"download": request.form.get("download") == "on"}
        if request.form.get("data_start"):
            payload["data_start"] = request.form["data_start"].strip()
        api.post_patch(f"/symbols/{symbol}", payload)
        flash(f"{symbol} 已更新", "ok")
    except api.ApiError as e:
        flash(f"更新失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/<symbol>/reverify")
def reverify(symbol):
    """重新向券商校验并刷新精度 (等价于重新登记同名品种; 异步, 1~2 分钟出结果)"""
    try:
        result = api.post("/symbols", {"symbol": symbol})
        flash(f"{result['symbol']} {result.get('hint', '已提交重新校验')}", "ok")
    except api.ApiError as e:
        flash(f"校验失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/<symbol>/purge")
def purge(symbol):
    """清空该品种全部历史 K线 (删登记前的必经步骤, 也用于清孤儿)"""
    try:
        result = api.delete(f"/symbols/{symbol}/data")
        flash(f"{symbol} 已清空 {result['deleted_bars']:,} 根历史数据", "ok")
    except api.ApiError as e:
        flash(f"清空失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))


@bp.post("/<symbol>/delete")
def delete(symbol):
    """删除登记 (api 侧: 有数据会拒绝, 需先清空)"""
    try:
        api.delete(f"/symbols/{symbol}")
        flash(f"{symbol} 已删除", "ok")
    except api.ApiError as e:
        flash(f"删除失败: {e}", "error")
    return redirect(request.referrer or url_for("symbols.index"))
