"""v0.6 自动化筛选-oos_v2 页(可插拔测试功能): 判据配置 + 运行(点名诊断/全池) + 报告回看。
只调 api(routes/oos_v2.py), 移除 = 删本文件 + app.py 注册两行 + base.html 导航一行。
时间显示: created_at 原样透传, 模板 m.lts() 交给浏览器转本地时区(全站统一机制)。"""
import csv
import io
import json
from datetime import datetime, timezone

from flask import (Blueprint, Response, flash, jsonify, redirect, render_template, request,
                   url_for)

import api_client as api

bp = Blueprint("oos_v2", __name__)


def _page_data():
    """页面基础数据(判据含实际日期/历史报告) — index 与点名诊断直渲共用"""
    return {"params": api.get("/oos_v2/params"),
            "reports": api.get("/oos_v2/reports")["reports"], "report": None}


@bp.get("/oos-v2")
def index():
    # 报告明细服务端分页 + 服务端排序(照 v1 定版: 几千行进浏览器会卡死, 前端只能排当页是假排序)
    per = request.args.get("per", 50, type=int)
    page = max(request.args.get("page", 1, type=int), 1)
    verdict = request.args.get("verdict", "")
    sort = request.args.get("sort", "")
    sdir = request.args.get("dir", "desc")
    try:
        data = _page_data()
        rid = request.args.get("report", type=int)
        if rid:
            data["report"] = api.get(f"/oos_v2/reports/{rid}", limit=per,
                                     offset=(page - 1) * per,
                                     **({"verdict": verdict} if verdict else {}),
                                     **({"sort": sort, "dir": sdir} if sort else {}))
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
        data = {"params": None, "reports": [], "report": None}
    return render_template("oos_v2.html", page=page, per=per, verdict=verdict,
                           sort=sort, sdir=sdir, **data)


@bp.get("/oos-v2/progress")
def progress():
    try:
        return jsonify(api.get("/oos_v2/progress"))
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.get("/oos-v2/plan")
def plan():
    try:
        params = {k: v for k, v in request.args.items() if v}
        return jsonify(api.get("/oos_v2/plan", **params))
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.post("/oos-v2/stop")
def stop():
    """停止当前全池批次: 删空队列(不出报告不打标签); 在跑的把手头回测跑完即自然结束"""
    try:
        r = api.post("/oos_v2/stop")
        return jsonify({"message": f"已停止, 删除 {r['deleted']} 个任务(不出报告)"})
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.post("/oos-v2/params")
def save_params():
    """保存判据(整包 PUT): 段定义每期一行(训练/测试起止 + 该期PF) + 全局三项。
    校验在 api(services/oos_v2.cfg_params), 不合法 400 不落库。
    AJAX 提交(data-post-ajax)回 JSON 就地 ✓/✗ 不刷页; 老式提交回 redirect 兜底。"""
    is_ajax = request.headers.get("X-Requested-With") == "fetch"
    try:
        segments = []
        for i in range(int(request.form.get("seg_count", 0))):
            def g(field):
                return (request.form.get(f"seg{i}_{field}") or "").strip()
            mp = g("min_pf")
            segments.append({
                "name": g("name"), "label": g("label") or g("name"),
                "train": [float(g("train_from")), float(g("train_to"))],
                "test": [float(g("test_from")), float(g("test_to"))],
                "min_pf": float(mp) if mp else None})
        r = api.put("/oos_v2/params", {
            "segments": segments,
            "default_pf": float(request.form.get("default_pf", 1)),
            "min_seg_trades": int(request.form.get("min_seg_trades", 10)),
            "batch_limit": int(request.form.get("batch_limit", 50)),
            "reuse_days": int(request.form.get("reuse_days", 7))})
        segs = " · ".join(
            f"{s['label']} 训{s['train'][0]:g}→{s['train'][1]:g} 测{s['test'][0]:g}→{s['test'][1]:g}"
            + (f" PF>{s['min_pf']:g}" if s.get("min_pf") is not None else "")
            for s in r["segments"])
        msg = (f"已保存: {segs} · 默认PF>{r['default_pf']:g}"
               f" · 样本提示<{r['min_seg_trades']}笔 · 单次上限{r['batch_limit']}"
               + (f" · 复用{r['reuse_days']}天" if r["reuse_days"] else " · 复用已关"))
        if is_ajax:
            return jsonify({"message": "已保存"})
        flash(msg, "success")
    except ValueError:
        if is_ajax:
            return jsonify({"error": "判据各项需为数字(年数可小数)"}), 400
        flash("判据各项需为数字(年数可小数)", "error")
    except api.ApiError as e:
        if is_ajax:
            return jsonify({"error": str(e)}), 400
        flash(f"保存失败: {e}", "error")
    return redirect(url_for("oos_v2.index"))


@bp.post("/oos-v2/run")
def run():
    payload = {"mode": request.form.get("mode", "preview")}
    try:
        ids_raw = (request.form.get("ids") or "").strip()
        if ids_raw:   # 点名 = 只读诊断; 不填 = 全部未筛过的空闲策略(第4步接队列)
            payload["ids"] = [int(x) for x in ids_raw.replace("，", ",").split(",") if x.strip()]
        if (request.form.get("task") or "").strip():
            payload["task"] = request.form["task"].strip()
        if (request.form.get("limit") or "").strip():
            payload["limit"] = int(request.form["limit"])
    except ValueError:
        flash("ID 列表/单次上限需为整数", "error")
        return redirect(url_for("oos_v2.index"))
    try:
        # 点名诊断 = api 内同步跑完才回(每策略一发20年回测 ~10s, 给足超时);
        # 全池清理 = 投队列秒回(第4步接)
        r = api.post("/oos_v2/run", payload, timeout=570)
        if r.get("queued"):
            flash(f"已投队列: {r['jobs']} 个回测任务 · "
                  f"{'预览' if r['mode'] == 'preview' else '执行'} · 锚点 {r['anchor']}"
                  + (f" · 复用{r['reused']}个(不重跑, 收尾一并判定)" if r.get("reused") else "")
                  + (f" · 跳过{r['skipped']}" if r.get("skipped") else "")
                  + (f" · 未跑{r['not_run']}(超单次上限)" if r.get("not_run") else "")
                  + " — worker 并行跑, 跑完自动判定出报告(可以关页面)", "success")
            return redirect(url_for("oos_v2.index"))
        s = r["summary"]
        flash(("预览" if r["mode"] == "preview" else "已执行")
              + (f" · 报告#{r['report_id']}" if r.get("report_id") else " · 点名诊断(未入库)")
              + f" · 锚点 {r['anchor']}:"
              + f" 共{s['total']} 通过{s['passed']} 未过{s['failed']} 跳过{s['skipped']}"
              + (f" ！样本不足{s['warned']}" if s.get("warned") else ""), "success")
        if r.get("report_id"):
            return redirect(url_for("oos_v2.index", report=r["report_id"]))
        # 点名 = 只读诊断不入库: 结果只活在本次响应里, 直接渲染(刷新即消失, 库里零痕迹)
        data = _page_data()
        data["report"] = {"id": None, "created_at": datetime.now(timezone.utc).isoformat(),
                          "mode": r["mode"], "anchor": r["anchor"], "scope": r["scope"],
                          "params": r["params"], "summary": s, "details": r["details"],
                          "total": len(r["details"]), "offset": 0, "limit": len(r["details"])}
        return render_template("oos_v2.html", page=1, per=len(r["details"]) or 1,
                               verdict="", sort="", sdir="desc", **data)
    except api.ApiError as e:
        flash(f"筛选失败: {e}", "error")
        return redirect(url_for("oos_v2.index"))


def _full_report(report_id: int) -> dict:
    """走 api 全量取(limit 分页拉满), 不受页面分页/排序影响 — 下载专用"""
    rep = api.get(f"/oos_v2/reports/{report_id}", limit=200, offset=0)
    total = rep.get("total") or 0
    details = list(rep.get("details") or [])
    while len(details) < total:
        batch = api.get(f"/oos_v2/reports/{report_id}",
                        limit=200, offset=len(details)).get("details") or []
        if not batch:
            break
        details += batch
    rep["details"] = details
    return rep


def _seg_cell(st: dict):
    """段读数 → CSV 单元(PF): 0笔="" , 无亏损=∞"""
    if not st or not st.get("n"):
        return ""
    return "∞" if st.get("pf") is None else st["pf"]


@bp.get("/oos-v2/report/<int:report_id>.<fmt>")
def report_download(report_id: int, fmt: str):
    """报告下载: .json = 全量原样(六段深层 + 判据快照, 贴给 AI 找规律)
    / .csv = 结论级一行一策略(Excel 友好, 段列按报告自己的段定义动态展开)"""
    if fmt not in ("json", "csv"):
        return {"error": "只支持 .json / .csv"}, 400
    try:
        rep = _full_report(report_id)
    except api.ApiError as e:
        return {"error": str(e)}, 502
    if fmt == "json":
        return Response(json.dumps(rep, ensure_ascii=False, indent=1, default=str),
                        mimetype="application/json; charset=utf-8", headers={
                            "Content-Disposition":
                                f'attachment; filename="oos_v2_{report_id}.json"'})
    segs = rep["params"]["segments"]
    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["报告", "锚点", "策略ID", "名称", "品种", "状态", "结论", "原因",
            "总笔数", "总净点", "总PF"]
    for s in segs:
        for part_cn in ("训练", "测试"):
            head += [f"{s['label']}{part_cn}{x}" for x in ("PF", "净点", "笔数", "回撤")]
    w.writerow(head)
    for d in rep["details"]:
        tot = d.get("total") or {}
        row = [report_id, rep.get("anchor"), d.get("id"), d.get("name"), d.get("symbol"),
               d.get("status"),
               {"pass": "PASS", "fail": "FAIL", "skip": "跳过"}.get(d.get("verdict"), ""),
               d.get("reason") or "",
               tot.get("n", ""), tot.get("net", ""), _seg_cell(tot)]
        pers = {p["name"]: p for p in (d.get("periods") or [])}
        for s in segs:
            per = pers.get(s["name"]) or {}
            for part in ("train", "test"):
                st = per.get(part) or {}
                row += [_seg_cell(st), st.get("net", ""), st.get("n", ""), st.get("dd", "")]
        w.writerow(row)
    return Response("\ufeff" + buf.getvalue(),   # BOM: Excel 打开中文不乱码
                    mimetype="text/csv; charset=utf-8", headers={
                        "Content-Disposition":
                            f'attachment; filename="oos_v2_{report_id}.csv"'})
