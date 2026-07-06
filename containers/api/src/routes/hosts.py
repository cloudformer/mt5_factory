"""/hosts — Windows worker 管理

职责: worker 注册(手动/自动上报)、启停、删除、角色校验、事件历史、远程下发 MT5 账户。
状态(ONLINE/OFFLINE)由 services.sync.heartbeat_loop 维护, 这里只读写注册信息。

规则: demo 与 live 不能同机 (一个 MT5 终端只能登录一个账户)。
"""
import logging

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import sync

logger = logging.getLogger("hosts")
router = APIRouter()

VALID_ROLES = {"download", "demo", "live"}


def _validate_roles(roles: list[str]):
    if not set(roles) <= VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"roles must be subset of {sorted(VALID_ROLES)}")
    if {"demo", "live"} <= set(roles):
        raise HTTPException(status_code=400,
                            detail="同一台主机不能同时担任 demo 和 live (一个MT5终端只能登录一个账户)")


@router.get("/hosts")
async def list_hosts(request: Request):
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, host, port, roles, account_type, enabled, status,"
        "       created_at, online_at, offline_at, last_heartbeat"
        "  FROM mt5_hosts ORDER BY id")
    return {"hosts": [dict(r) for r in rows]}


@router.get("/hosts/{host_id}/events")
async def host_events(host_id: int, request: Request, limit: int = 100):
    """worker 生命周期历史 (注册/上下线/启停/角色变更/账户下发)"""
    rows = await request.app.state.pool.fetch(
        "SELECT event, detail, created_at FROM mt5_host_events"
        " WHERE host_id=$1 ORDER BY created_at DESC LIMIT $2", host_id, limit)
    return {"events": [dict(r) for r in rows]}


class HostCreate(BaseModel):
    name: str
    host: str
    port: int = 8020
    roles: list[str] = ["download"]
    account_type: str = "DEMO"


@router.post("/hosts")
async def create_host(req: HostCreate, request: Request):
    """手动注册 worker"""
    _validate_roles(req.roles)
    if req.account_type not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="account_type must be DEMO or REAL")
    pool = request.app.state.pool
    try:
        row = await pool.fetchrow(
            "INSERT INTO mt5_hosts (name, host, port, roles, account_type)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING *",
            req.name, req.host, req.port, req.roles, req.account_type)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="name or host:port already registered")
    await sync.log_host_event(pool, row["id"], "REGISTERED", {"source": "manual"})
    return dict(row)


class AnnounceRequest(BaseModel):
    name: str
    host: str
    port: int = 8020


@router.post("/hosts/announce")
async def announce_host(req: AnnounceRequest, request: Request):
    """worker 自动注册: bridge 启动后周期性自报家门。
    新 worker 以 download 角色入册 (demo/live 必须由人指派);
    已存在则只刷新心跳, 不覆盖人工配置。"""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "INSERT INTO mt5_hosts (name, host, port, roles, last_heartbeat)"
        " VALUES ($1, $2, $3, '{download}', now())"
        " ON CONFLICT (host, port) DO UPDATE SET last_heartbeat = now()"
        " RETURNING id, name, roles, enabled, (xmax = 0) AS inserted",
        req.name, req.host, req.port)
    if row["inserted"]:
        await sync.log_host_event(pool, row["id"], "REGISTERED", {"source": "announce"})
    return {k: row[k] for k in ("id", "name", "roles", "enabled")}


class HostUpdate(BaseModel):
    enabled: bool | None = None
    roles: list[str] | None = None
    host: str | None = None
    port: int | None = None
    account_type: str | None = None


@router.patch("/hosts/{host_id}")
async def update_host(host_id: int, req: HostUpdate, request: Request):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if "roles" in fields:
        _validate_roles(fields["roles"])
    pool = request.app.state.pool
    old = await pool.fetchrow("SELECT enabled, roles FROM mt5_hosts WHERE id=$1", host_id)
    if old is None:
        raise HTTPException(status_code=404, detail="host not found")
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    row = await pool.fetchrow(
        f"UPDATE mt5_hosts SET {sets} WHERE id = $1 RETURNING *", host_id, *fields.values())
    if "enabled" in fields and fields["enabled"] != old["enabled"]:
        await sync.log_host_event(pool, host_id, "ENABLED" if fields["enabled"] else "DISABLED")
    if "roles" in fields and set(fields["roles"]) != set(old["roles"]):
        await sync.log_host_event(pool, host_id, "ROLES_CHANGED",
                                  {"from": list(old["roles"]), "to": fields["roles"]})
    return dict(row)


@router.delete("/hosts/{host_id}")
async def delete_host(host_id: int, request: Request):
    row = await request.app.state.pool.fetchrow(
        "DELETE FROM mt5_hosts WHERE id=$1 RETURNING name", host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="host not found")
    return {"deleted": row["name"]}


class ConnectRequest(BaseModel):
    login: int
    password: str
    server: str


@router.post("/hosts/{host_id}/connect")
async def connect_host(host_id: int, req: ConnectRequest, request: Request):
    """向 worker 远程下发 MT5 账户 (转发到 bridge /connect)"""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT host, port FROM mt5_hosts WHERE id=$1 AND enabled", host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="host not found or disabled")
    headers = {"X-API-Key": sync.BRIDGE_API_KEY} if sync.BRIDGE_API_KEY else {}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        try:
            r = await client.post(
                f"http://{row['host']}:{row['port']}/connect", json=req.model_dump())
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"bridge unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json().get("detail"))
    await pool.execute(
        "UPDATE mt5_hosts SET mt5_login=$2, mt5_server=$3 WHERE id=$1",
        host_id, req.login, req.server)
    await sync.log_host_event(pool, host_id, "ACCOUNT_SET",
                              {"login": req.login, "server": req.server})
    return r.json()
