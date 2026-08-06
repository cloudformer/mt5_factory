"""v0.5 Regime 筛选页(可插拔测试功能): 判据参数 + 运行(预览/执行) + 历史报告回看。
只调 api(regime_screen.py), 移除 = 删本文件 + app.py 注册两行 + base.html 导航一行。
时间显示: created_at 原样透传, 模板 m.lts() 交给浏览器转本地时区(全站统一机制)。"""
from datetime import datetime, timezone

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("regime_screen", __name__)


def _page_data():
    """页面基础数据(判据/版本/历史报告) — index 与点名诊断直渲共用"""
    data = {"params": None, "versions": None, "reports": [], "report": None}
    cfg = api.get("/config")["config"]
    data["params"] = cfg.get("regime_screen") or {}
    data["versions"] = api.get("/regime/versions")
    data["reports"] = api.get("/regime_screen/reports")["reports"]
    return data


@bp.get("/regime-screen")
def index():
    # 报告明细服务端分页(2026-08-05 实测定: 500行全量进浏览器会卡死);
    # 筛选/翻页走全站通用局部刷新(tables.js data-ajax): 重取本页 → 只换 #rs-report-box
    per = request.args.get("per", 50, type=int)
    page = max(request.args.get("page", 1, type=int), 1)
    verdict = request.args.get("verdict", "")
    try:
        data = _page_data()
        rid = request.args.get("report", type=int)
        if rid:
            data["report"] = api.get(f"/regime_screen/reports/{rid}", limit=per,
                                     offset=(page - 1) * per,
                                     **({"verdict": verdict} if verdict else {}))
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
        data = {"params": None, "versions": None, "reports": [], "report": None}
    return render_template("regime_screen.html", page=page, per=per, verdict=verdict, **data)


@bp.get("/regime-screen/progress")
def progress():
    """运行进度(页面 AJAX 轮询): 透传 api 内存进度"""
    try:
        return jsonify(api.get("/regime_screen/progress"))
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.get("/regime-screen/detail/<int:report_id>/<int:sid>")
def detail(report_id: int, sid: int):
    """单策略深层数字(展开时按需取, 2026-08-05 Frank 定): 每展开一个只加载那一个"""
    try:
        return jsonify(api.get(f"/regime_screen/reports/{report_id}/strategy/{sid}"))
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.get("/regime-screen/plan")
def plan():
    """运行预估(页面预览行 AJAX): 透传 api, 匹配多少/可判多少/各类跳过多少"""
    try:
        params = {k: v for k, v in request.args.items() if v}
        return jsonify(api.get("/regime_screen/plan", **params))
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.post("/regime-screen/params")
def save_params():
    try:
        bs = [float(x) for x in
              request.form.get("boundaries", "").replace("，", ",").split(",") if x.strip()]
        r = api.post("/regime_screen/params", {
            "window_years": float(request.form.get("window_years", 5)),
            "boundaries_years": bs,
            "min_cell_trades": int(request.form.get("min_cell_trades", 5)),
            "min_pass_cells": int(request.form.get("min_pass_cells", 1)),
            "min_net_points": float(request.form.get("min_net_points", 0)),
            "min_pf": float(request.form.get("min_pf", 1))})
        flash(f"判据已保存: 总计 {r['window_years']:g} 年 · 切分 近{r['boundaries_years']}年vs剩余"
              f" · 地板 {r['min_cell_trades']} 笔 · ≥{r['min_pass_cells']} 格"
              f" · 净点>{r['min_net_points']:g} · PF>{r['min_pf']:g}", "success")
    except ValueError:
        flash("判据各项需为数字(切分点逗号分隔, 可小数)", "error")
    except api.ApiError as e:
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("regime_screen.index"))


@bp.post("/regime-screen/run")
def run():
    payload = {"mode": request.form.get("mode", "preview"),
               "symbols": request.form.get("symbols", "main")}
    try:
        ids_raw = (request.form.get("ids") or "").strip()
        if ids_raw:   # 点名小范围; 不填 = 全部未筛过的空闲策略(轮番清理)
            payload["ids"] = [int(x) for x in ids_raw.replace("，", ",").split(",") if x.strip()]
        if (request.form.get("task") or "").strip():
            payload["task"] = request.form["task"].strip()
        if request.form.get("version"):
            payload["version"] = int(request.form["version"])
        if (request.form.get("limit") or "").strip():
            payload["limit"] = int(request.form["limit"])
    except ValueError:
        flash("ID 列表/单次上限需为整数", "error")
        return redirect(url_for("regime_screen.index"))
    try:
        # 全池清理 = 投队列秒回(worker 并行跑, 页面轮询进度);
        # 点名诊断 = api 内同步跑完才回(几个 ID, 给足超时)
        r = api.post("/regime_screen/run", payload, timeout=570)
        if r.get("queued"):
            flash(f"已投队列: {r['jobs']} 个回测任务({r['strategies']} 策略) · "
                  f"{'预览' if r['mode'] == 'preview' else '执行'} · regime v{r['version']}"
                  + (f" · 跳过{r['skipped']}" if r.get("skipped") else "")
                  + (f" · 未跑{r['not_run']}(超单次上限)" if r.get("not_run") else "")
                  + " — worker 并行跑, 跑完自动判定出报告(可以关页面)", "success")
            return redirect(url_for("regime_screen.index"))
        s = r["summary"]
        msg = (("预览" if r["mode"] == "preview" else "已执行")
               + (f" · 报告#{r['report_id']}" if r.get("report_id") else " · 点名诊断(未入库)")
               + f" (regime v{r['version']}):"
               + f" 共{s['total']} 通过{s['passed']} 未过{s['failed']}"
               + (f"(归档{s['archived']})" if r["mode"] == "execute" else "")
               + f" 跳过{s['skipped']}"
               + (f" 未跑{s['not_run']}(超单次上限, 下次再跑)" if s.get("not_run") else ""))
        flash(msg, "success")
        if r.get("report_id"):
            return redirect(url_for("regime_screen.index", report=r["report_id"]))
        # 点名 = 只读诊断不入库: 结果只活在本次响应里, 直接渲染(刷新即消失, 库里零痕迹)
        # 点名范围小(按ID), 不分页; 深层数字随响应内嵌, 展开零请求
        data = _page_data()
        data["report"] = {"id": None, "created_at": datetime.now(timezone.utc).isoformat(),
                          "mode": r["mode"], "version_id": r["version"], "scope": r["scope"],
                          "params": r["params"], "summary": s, "details": r["details"],
                          "total": len(r["details"]), "offset": 0, "limit": len(r["details"])}
        return render_template("regime_screen.html", page=1, per=len(r["details"]) or 1,
                               verdict="", **data)
    except api.ApiError as e:
        flash(f"筛选失败: {e}", "error")
        return redirect(url_for("regime_screen.index"))
