"""用户管理后台(v5.5): 建用户/启停/发两类key/用量观察 — 机制在 5.1~5.3 的表,
这里只是操作台。认证 v5.6 才接: 当前与全站同一安全水位(内网单人), 5.6 前不要暴露公网。"""
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import api_client as api

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
def owner_only():
    """管理台门禁(v5.6 简单权限): /admin/* 只认 owner(id 1), 身份即令牌
    (登录前 = 右上角切换的身份; 上登录后这里换成真凭据判断, 路由零改动)。
    switch_user 例外 — 它是换身份的口, 拦了就没人能切成 owner 了。"""
    if request.endpoint == "admin.switch_user":
        return
    if session.get("dev_user_id") != 1:
        flash("管理员页面仅 owner(admin)可用 — 右上角切换身份", "error")
        return redirect(url_for("dashboard.index"))


@bp.get("/users")
def users_page():
    users, keys, wkeys, hosts = [], [], [], []
    usage_view = {}   # {user_id: {metric: {"today": n, "total": n}}}
    try:
        users = api.get("/users")["users"]
        keys = api.get("/keys")["keys"]
        wkeys = api.get("/worker_keys")["worker_keys"]
        hosts = api.get("/hosts")["hosts"]
        for r in api.get("/usage")["usage"]:
            usage_view.setdefault(r["user_id"], {})[r["metric"]] = {
                "today": r["today"], "total": r["used_total"]}
    except api.ApiError as e:
        flash(f"api 不可用: {e}", "error")
    return render_template("admin_users.html", users=users, keys=keys, wkeys=wkeys,
                           hosts=hosts, usage_view=usage_view)


@bp.post("/switch_user")
def switch_user():
    """开发模式身份切换(v5.6 登录前的过渡): 只改右上角标识, 不做任何过滤;
    全部测试好了才加登录页(2026-07-25 与 Frank 定)"""
    try:
        session["dev_user_id"] = int(request.form["user_id"])
    except (ValueError, KeyError):
        flash("参数错误", "error")
    return redirect(request.referrer or url_for("admin.users_page"))


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


# 配置覆盖(user_config)编辑路由已撤(2026-07-25): 原始JSON键值编辑不好用 —
# 机制保留在库(034), 将来做成配置页式的按用户编辑(随 5.6)
