"""筛选·策略×regime 映射规律 页(2026-08-11 与 Frank 定稿, 可插拔)。

只调 api(routes/regime_map.py); 移除 = 删本文件 + app.py 注册两行 + base.html 导航一行。
铁律: 各版本独立评估, 页面绝不做跨版本排名/择优。
"""
import json as _json

from flask import (Blueprint, Response, flash, jsonify, redirect, render_template,
                   request, url_for)

import api_client as api

bp = Blueprint("regime_map", __name__)


@bp.get("/regime-map")
def index():
    """运行框(判据走表单不落库) + 报告回看 + 单策略下钻"""
    rid = request.args.get("report", type=int)
    verdict = request.args.get("verdict") or None
    page = max(request.args.get("page", 1, type=int), 1)          # 报告【内部】明细翻页
    rpage = max(request.args.get("rpage", 1, type=int), 1)        # 报告【列表】翻页(独立)
    per = min(max(request.args.get("per", 50, type=int), 10), 200)
    data = {"reports": [], "report": None, "batches": [], "rlist": {}}
    try:
        # 本页只能算有回测行的策略 → 下拉也只列这些, 免得选中没法算的批次收 400
        data["batches"] = api.get("/strategy_batches", only_tested=1)["batches"]
        data["rlist"] = api.get("/regime_map/reports", page=rpage, per=30)
        data["reports"] = data["rlist"]["reports"]
        if rid is None and data["reports"]:
            rid = data["reports"][0]["id"]
        if rid:
            data["report"] = api.get(f"/regime_map/reports/{rid}",
                                     **{"verdict": verdict} if verdict else {},
                                     page=page, per=per)
    except api.ApiError as e:
        flash(f"载入失败: {e}", "error")
    return render_template("regime_map.html", data=data, rid=rid, verdict=verdict,
                           page=page, per=per, rpage=rpage)


@bp.post("/regime-map/run")
def run():
    """跑批: 判据 + 范围一次提交(判据不落库, 随报告存快照)"""
    def num(name, default, cast=float):
        raw = (request.form.get(name) or "").strip()
        return cast(raw) if raw else default
    payload = {
        "permutations": num("permutations", 1000, int),
        "sig_p": num("sig_p", 0.05),
        "min_enrich": num("min_enrich", 1.5),
        "min_cell_trades": num("min_cell_trades", 30, int),
        "min_tier_cell": num("min_tier_cell", 10, int),
        "min_tier_pct": num("min_tier_pct", 10.0),
        "limit": num("limit", 200, int),
        "task": (request.form.get("task") or "").strip() or None,
    }
    for k in ("template", "symbol", "status", "basis"):
        v = (request.form.get(k) or "").strip()
        if v:
            payload[k] = v
    ids = (request.form.get("ids") or "").strip()
    if ids:
        try:
            payload["ids"] = api.parse_ids(ids)
        except ValueError:
            flash("ID 串需为逗号分隔的数字", "error")
            return redirect(url_for("regime_map.index"))
    try:
        r = api.post("/regime_map/run", payload)
        flash(f"已投递: {r['strategies']} 个策略 × {len(r['versions'])} 个版本"
              f" = {r['chunks']} 块并行计算(纯计算复用回测行, 不重跑引擎)", "ok")
    except (api.ApiError, ValueError) as e:
        flash(f"投递失败: {e}", "error")
    return redirect(url_for("regime_map.index"))


@bp.post("/regime-map/stop")
def stop():
    """停止当前批次(四页同款): 删空队列 = 不出报告"""
    try:
        r = api.post("/regime_map/stop", {})
        return jsonify({"message": f"已停止, 删除 {r['deleted']} 个任务(不出报告)"})
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.get("/regime-map/progress")
def progress():
    try:
        return jsonify(api.get("/regime_map/progress"))
    except api.ApiError as e:
        return jsonify({"error": str(e)}), 502


@bp.get("/regime-map/reports/<int:rid>.json")
def report_json(rid: int):
    """整份报告 JSON(本地深挖/喂 AI 用)"""
    try:
        d = api.get(f"/regime_map/reports/{rid}", per=100000)
        return app_json(d, f"regime_map-{rid}.json")
    except api.ApiError as e:
        return {"error": str(e)}, 502


def app_json(obj, filename: str):
    return Response(_json.dumps(obj, ensure_ascii=False, indent=1, default=str),
                    mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
