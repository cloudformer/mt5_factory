"""用户管理后台(v5.5): 建用户/启停/发key/配置覆盖/划worker 页面化 — 机制在 5.1~5.3 的表,
这里只是操作台。认证 v5.6 才接: 当前与全站同一安全水位(内网单人), 5.6 前不要暴露公网。"""
import json as _json

from flask import Blueprint, flash, redirect, render_template, request, url_for

import api_client as api

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.get("/users")
def users_page():
    users, keys, wkeys, hosts, overrides, cfg_keys = [], [], [], [], [], []
    try:
        users = api.get("/users")["users"]
        keys = api.get("/keys")["keys"]
        wkeys = api.get("/worker_keys")["worker_keys"]
        hosts = api.get("/hosts")["hosts"]
        overrides = api.get("/user_config")["overrides"]
        cfg_keys = sorted(api.get("/config")["config"].keys())
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("admin_users.html", users=users, keys=keys, wkeys=wkeys,
                           hosts=hosts, overrides=overrides, cfg_keys=cfg_keys)


@bp.post("/users/create")
def create_user():
    try:
        r = api.post("/users", {"name": request.form.get("name", "").strip()})
        flash(f"用户 #{r['id']} {r['name']} 已创建 — 配置零行(实时跟随全局默认)、策略零行", "ok")
    except api.ApiError as e:
        flash(f"建用户失败: {e}", "error")
    return redirect(url_for("admin.users_page"))


@bp.post("/users/<int:user_id>/toggle")
def toggle_user(user_id: int):
    try:
        r = api.post(f"/users/{user_id}/enabled",
                     {"enabled": request.form.get("enabled") == "1"})
        flash(f"{r['name']} → {'启用' if r['enabled'] else '停用(v5.6 接认证后即全面失效)'}", "ok")
    except api.ApiError as e:
        flash(f"操作失败: {e}", "error")
    return redirect(url_for("admin.users_page"))


@bp.post("/users/<int:user_id>/issue_key")
def issue_key(user_id: int):
    try:
        r = api.post(f"/users/{user_id}/keys",
                     {"name": request.form.get("name", "").strip() or None})
        flash(f"key 已签发给 {r['user']}(明文只此一次, 立即复制保存): {r['key']}", "ok")
    except api.ApiError as e:
        flash(f"签发失败: {e}", "error")
    return redirect(url_for("admin.users_page"))


@bp.post("/keys/<int:key_id>/toggle")
def toggle_key(key_id: int):
    try:
        r = api.post(f"/keys/{key_id}/enabled",
                     {"enabled": request.form.get("enabled") == "1"})
        flash(f"key #{r['id']} → {'恢复' if r['enabled'] else '已吊销'}", "ok")
    except api.ApiError as e:
        flash(f"操作失败: {e}", "error")
    return redirect(url_for("admin.users_page"))


@bp.post("/worker_keys/issue")
def issue_worker_key():
    try:
        r = api.post(f"/users/{int(request.form['user_id'])}/worker_keys",
                     {"name": request.form.get("name", "").strip() or None})
        flash(f"worker 钥匙已签发给 {r['user']}(明文只此一次): {r['key']}"
              " — 写进该机 env 的 WORKER_KEY, 首台 announce 的机器与它绑定", "ok")
    except (ValueError, KeyError):
        flash("user_id 格式错误", "error")
    except api.ApiError as e:
        flash(f"签发失败: {e}", "error")
    return redirect(url_for("admin.users_page"))


@bp.post("/worker_keys/<int:key_id>/toggle")
def toggle_worker_key(key_id: int):
    try:
        r = api.post(f"/worker_keys/{key_id}/enabled",
                     {"enabled": request.form.get("enabled") == "1"})
        flash(f"worker 钥匙 #{r['id']} → {'恢复(需机器重新 announce 绑定)' if r['enabled'] else '已吊销并解绑'}", "ok")
    except api.ApiError as e:
        flash(f"操作失败: {e}", "error")
    return redirect(url_for("admin.users_page"))


@bp.post("/user_config/set")
def set_user_config():
    raw = request.form.get("value", "").strip()
    try:
        value = _json.loads(raw)   # 数字/数组/对象照原样; 不是合法 JSON 就按字符串存
    except ValueError:
        value = raw
    try:
        uid = int(request.form["user_id"])
        key = request.form["key"].strip()
        api.put(f"/users/{uid}/config/{key}", {"value": value})
        flash(f"用户 #{uid} 覆盖 {key} = {value}", "ok")
    except (ValueError, KeyError):
        flash("参数格式错误", "error")
    except api.ApiError as e:
        flash(f"写覆盖失败: {e}", "error")
    return redirect(url_for("admin.users_page"))


@bp.post("/user_config/del")
def del_user_config():
    try:
        uid = int(request.form["user_id"])
        key = request.form["key"]
        api.delete(f"/users/{uid}/config/{key}")
        flash(f"用户 #{uid} 的 {key} 覆盖已删 — 回落全局默认", "ok")
    except (ValueError, KeyError):
        flash("参数格式错误", "error")
    except api.ApiError as e:
        flash(f"删除失败: {e}", "error")
    return redirect(url_for("admin.users_page"))
