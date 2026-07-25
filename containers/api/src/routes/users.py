"""用户管理(v5.5 管理后台 api 侧): 建用户/启停/发key/配置覆盖 —— 机制复用 5.1~5.3 的表。
认证与权限在 v5.6 才接(当前与全站同水位); key 只存 sha256 哈希, 明文仅签发时返回一次。"""
import hashlib
import logging
import secrets
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger("users")


@router.get("/users")
async def list_users(request: Request):
    """用户列表 + 名下资产计数(策略/worker/启用key)"""
    rows = await request.app.state.pool.fetch(
        "SELECT u.id, u.name, u.enabled, u.created_at,"
        "  (SELECT count(*) FROM strategies s WHERE s.owner_id = u.id) AS strategies,"
        "  (SELECT count(*) FROM mt5_hosts h WHERE h.owner_id = u.id) AS workers,"
        "  (SELECT count(*) FROM api_keys k WHERE k.user_id = u.id AND k.enabled) AS keys"
        " FROM users u ORDER BY u.id")
    return {"users": [dict(r) for r in rows]}


class UserRequest(BaseModel):
    name: str


@router.post("/users")
async def create_user(req: UserRequest, request: Request):
    """建用户 = 插一行(配置零行全走全局默认, 策略零行; 与 Frank 对齐的空覆盖层模型)"""
    name = req.name.strip()
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="用户名须 1~64 字符")
    try:
        row = await request.app.state.pool.fetchrow(
            "INSERT INTO users (name) VALUES ($1) RETURNING id, name", name)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=f"用户名 {name} 已存在")
    logger.info("user created #%d %s", row["id"], name)
    return dict(row)


class EnabledRequest(BaseModel):
    enabled: bool


@router.post("/users/{user_id}/enabled")
async def set_user_enabled(user_id: int, req: EnabledRequest, request: Request):
    """停用=一刀切(v5.6 接认证后 key/登录/worker 全失效); user 1(owner)不可停"""
    if user_id == 1 and not req.enabled:
        raise HTTPException(status_code=400, detail="user 1(owner)不可停用")
    row = await request.app.state.pool.fetchrow(
        "UPDATE users SET enabled=$2 WHERE id=$1 RETURNING id, name, enabled",
        user_id, req.enabled)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    logger.info("user #%d enabled -> %s", user_id, req.enabled)
    return dict(row)


class KeyRequest(BaseModel):
    name: Optional[str] = None   # 备注: 谁的哪把钥匙(网页/脚本各一把, 泄露吊销单把)


@router.post("/users/{user_id}/keys")
async def issue_key(user_id: int, req: KeyRequest, request: Request):
    """签发 api key: 服务端生成高熵随机串 → 只存 sha256 → 明文仅此响应一次"""
    u = await request.app.state.pool.fetchrow(
        "SELECT id, name FROM users WHERE id=$1", user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    token = "mt5_" + secrets.token_urlsafe(32)
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO api_keys (user_id, key_hash, name) VALUES ($1, $2, $3) RETURNING id",
        user_id, hashlib.sha256(token.encode()).hexdigest(), (req.name or "").strip() or None)
    logger.info("api key #%d issued for user #%d", row["id"], user_id)
    return {"id": row["id"], "user": u["name"], "key": token,
            "note": "明文只此一次, 请立即保存; 库里只有哈希, 丢了只能重发"}


@router.get("/keys")
async def list_keys(request: Request):
    rows = await request.app.state.pool.fetch(
        "SELECT k.id, k.user_id, u.name AS user, k.name, k.enabled,"
        "       k.last_used_at, k.created_at"
        "  FROM api_keys k JOIN users u ON u.id = k.user_id ORDER BY k.id")
    return {"keys": [dict(r) for r in rows]}


@router.post("/keys/{key_id}/enabled")
async def set_key_enabled(key_id: int, req: EnabledRequest, request: Request):
    """吊销/恢复单把 key(一人多把: 泄露只吊那把, 其余照用)"""
    row = await request.app.state.pool.fetchrow(
        "UPDATE api_keys SET enabled=$2 WHERE id=$1 RETURNING id, enabled",
        key_id, req.enabled)
    if row is None:
        raise HTTPException(status_code=404, detail="key not found")
    return dict(row)


@router.post("/users/{user_id}/worker_keys")
async def issue_worker_key(user_id: int, req: KeyRequest, request: Request):
    """签发 worker 钥匙(schema/040): 写进 Windows env(WORKER_KEY) → 机器 announce 带钥
    → 自动归该用户 + 与机器首绑。明文只此一次。"""
    u = await request.app.state.pool.fetchrow(
        "SELECT id, name FROM users WHERE id=$1", user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    token = "mt5wk_" + secrets.token_urlsafe(32)
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO worker_keys (user_id, key_hash, name) VALUES ($1, $2, $3) RETURNING id",
        user_id, hashlib.sha256(token.encode()).hexdigest(), (req.name or "").strip() or None)
    logger.info("worker key #%d issued for user #%d", row["id"], user_id)
    return {"id": row["id"], "user": u["name"], "key": token,
            "note": "写进该机 env 的 WORKER_KEY; 明文只此一次; 首台 announce 的机器与它绑定"}


@router.get("/worker_keys")
async def list_worker_keys(request: Request):
    rows = await request.app.state.pool.fetch(
        "SELECT w.id, w.user_id, u.name AS user, w.name, w.enabled, w.last_used_at,"
        "       w.created_at, w.host_id, h.name AS host"
        "  FROM worker_keys w JOIN users u ON u.id = w.user_id"
        "  LEFT JOIN mt5_hosts h ON h.id = w.host_id ORDER BY w.id")
    return {"worker_keys": [dict(r) for r in rows]}


@router.post("/worker_keys/{key_id}/enabled")
async def set_worker_key_enabled(key_id: int, req: EnabledRequest, request: Request):
    """吊销/恢复 worker 钥匙; 吊销自动解绑机器(轮换=吊旧发新, 新钥匙才能绑同一台机)"""
    row = await request.app.state.pool.fetchrow(
        "UPDATE worker_keys SET enabled=$2,"
        "  host_id = CASE WHEN $2 THEN host_id ELSE NULL END"
        " WHERE id=$1 RETURNING id, enabled",
        key_id, req.enabled)
    if row is None:
        raise HTTPException(status_code=404, detail="worker key not found")
    logger.info("worker key #%d enabled -> %s", key_id, req.enabled)
    return dict(row)


@router.get("/usage")
async def usage_summary(request: Request):
    """用量一览(usage_counters, 只记录不拦截): 每 user×指标 一行, 今日=day是今天才有效"""
    rows = await request.app.state.pool.fetch(
        "SELECT c.user_id, u.name AS user, c.metric, c.used_total,"
        "       CASE WHEN c.day = CURRENT_DATE THEN c.day_used ELSE 0 END AS today,"
        "       c.updated_at"
        "  FROM usage_counters c JOIN users u ON u.id = c.user_id"
        " ORDER BY c.user_id, c.metric")
    return {"usage": [dict(r) for r in rows]}


# user_config 的读写端点已撤(2026-07-25 Frank: 原始JSON键值编辑不好用) —
# 表与"空覆盖层+回落全局"机制保留(schema/034), 读取路径与配置页式的按用户编辑随 v5.6 一起做
