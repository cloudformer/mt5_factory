"""/hosts — Windows worker 管理

职责: worker 注册(手动/自动上报)、启停、删除、职能设置、事件历史、远程下发 MT5 账户。
状态(ONLINE/OFFLINE)由 services.sync.heartbeat_loop 维护, 这里只读写注册信息。

职能模型 (约束靠数据库结构): download BOOLEAN 是否下载;
runner = demo|live|NULL 跑什么策略 — 单字段天然保证 demo/live 互斥。
"""
import logging

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import sync

logger = logging.getLogger("hosts")
router = APIRouter()


def _validate_runner(runner: str | None):
    if runner is not None and runner not in ("demo", "live"):
        raise HTTPException(status_code=400, detail="runner must be demo, live or null")


async def _claim_account(pool, host_id: int, login: int, server: str):
    """铁律"不同 worker 不得共用 MT5 账户"的唯一实现: 把账户写进列,
    数据库唯一索引 (schema/002, 仅对 enabled 主机生效) 写失败即撞号 → 409。"""
    try:
        await pool.execute(
            "UPDATE mt5_hosts SET mt5_login=$2, mt5_server=$3 WHERE id=$1",
            host_id, login, server)
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"MT5 账户 {login}@{server} 已被其他启用的 worker 使用 — "
                   "铁律: 不同 worker 不得共用账户(同账户双跑会重复下单), 请换账户")


@router.get("/hosts")
async def list_hosts(request: Request):
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, host, port, download, runner, account_type, enabled, status,"
        "       created_at, online_at, offline_at, last_heartbeat, last_health"
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
    download: bool = True
    runner: str | None = None   # demo | live | None(不跑)
    account_type: str = "DEMO"


@router.post("/hosts")
async def create_host(req: HostCreate, request: Request):
    """手动注册 worker"""
    _validate_runner(req.runner)
    if req.account_type not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="account_type must be DEMO or REAL")
    pool = request.app.state.pool
    try:
        row = await pool.fetchrow(
            "INSERT INTO mt5_hosts (name, host, port, download, runner, account_type)"
            " VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            req.name, req.host, req.port, req.download, req.runner, req.account_type)
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
    身份 = name(计算机名); IP:port 只是当前地址, 每次刷新 —— DHCP/换网都不影响身份。
    新 worker 默认只承担下载 (runner 必须由人指派); 已存在则刷新地址+心跳, 不覆盖人工配置。"""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "INSERT INTO mt5_hosts (name, host, port, download, last_heartbeat)"
        " VALUES ($1, $2, $3, TRUE, now())"
        " ON CONFLICT (name) DO UPDATE SET host = $2, port = $3, last_heartbeat = now()"
        " RETURNING id, name, download, runner, enabled, (xmax = 0) AS inserted",
        req.name, req.host, req.port)
    if row["inserted"]:
        await sync.log_host_event(pool, row["id"], "REGISTERED", {"source": "announce"})
    return {k: row[k] for k in ("id", "name", "download", "runner", "enabled")}


class HostUpdate(BaseModel):
    enabled: bool | None = None
    download: bool | None = None
    runner: str | None = None   # 传 null 表示清除(不跑策略); 不传表示不改
    host: str | None = None
    port: int | None = None
    account_type: str | None = None


@router.patch("/hosts/{host_id}")
async def update_host(host_id: int, req: HostUpdate, request: Request):
    # exclude_unset: 区分"没传"和"传了null" — runner 传 null 是合法操作(取消跑策略)
    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if "runner" in fields:
        _validate_runner(fields["runner"])
    pool = request.app.state.pool
    old = await pool.fetchrow(
        "SELECT enabled, download, runner FROM mt5_hosts WHERE id=$1", host_id)
    if old is None:
        raise HTTPException(status_code=404, detail="host not found")
    # 职能互斥: 已指派 demo/live 的主机不能直接改投另一边, 必须先取消指派(runner=null)
    if ("runner" in fields and fields["runner"] and old["runner"]
            and fields["runner"] != old["runner"]):
        raise HTTPException(
            status_code=400,
            detail=f"该主机已指派为 {old['runner']}, 必须先取消指派才能改为 {fields['runner']}")
    # 铁律: 指派交易职能前, 把本机实际登录的账户(心跳回传)写进列 —
    # 数据库唯一索引撞号即 409 (克隆机自带旧账户的场景在这里被拦下)
    if "runner" in fields and fields["runner"]:
        hb = await pool.fetchrow(
            "SELECT (last_health->>'login')::bigint AS login, last_health->>'server' AS server"
            "  FROM mt5_hosts WHERE id=$1 AND last_health->>'login' IS NOT NULL", host_id)
        if hb:
            await _claim_account(pool, host_id, hb["login"], hb["server"])
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    try:
        row = await pool.fetchrow(
            f"UPDATE mt5_hosts SET {sets} WHERE id = $1 RETURNING *", host_id, *fields.values())
    except asyncpg.UniqueViolationError:
        # 重新启用主机时唯一索引会重新生效: 它的账户已被别机占用则拒绝启用
        raise HTTPException(status_code=409,
                            detail="该主机的 MT5 账户已被其他启用的 worker 使用 — 先换账户再启用")
    if "enabled" in fields and fields["enabled"] != old["enabled"]:
        await sync.log_host_event(pool, host_id, "ENABLED" if fields["enabled"] else "DISABLED")
    for key in ("download", "runner"):
        if key in fields and fields[key] != old[key]:
            await sync.log_host_event(pool, host_id, "ROLES_CHANGED",
                                      {"field": key, "from": old[key], "to": fields[key]})
    return dict(row)


@router.delete("/hosts/{host_id}")
async def delete_host(host_id: int, request: Request):
    row = await request.app.state.pool.fetchrow(
        "DELETE FROM mt5_hosts WHERE id=$1 RETURNING name", host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="host not found")
    return {"deleted": row["name"]}


@router.post("/hosts/{host_id}/restart")
async def host_restart(host_id: int, request: Request):
    """远程重启 worker 的 bridge/runner (不更新代码 — 更新在 Windows 上手动 update.bat)。
    转发到 bridge /restart, 那边写标志退出、看门狗接管。"""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT host, port FROM mt5_hosts WHERE id=$1 AND enabled", host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="host not found or disabled")
    headers = {"X-API-Key": sync.BRIDGE_API_KEY} if sync.BRIDGE_API_KEY else {}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        try:
            r = await client.post(f"http://{row['host']}:{row['port']}/restart")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"bridge unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code,
                            detail=r.json().get("detail", "bridge error"))
    await sync.log_host_event(pool, host_id, "RESTART")
    return r.json()


@router.get("/hosts/{host_id}/trades")
async def host_trades(host_id: int, request: Request, days: int = 30):
    """转发 worker 的 MT5 交易流水 (持仓+成交明细, 原样透传, web /mt5 页用)"""
    row = await request.app.state.pool.fetchrow(
        "SELECT host, port FROM mt5_hosts WHERE id=$1 AND enabled", host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="host not found or disabled")
    headers = {"X-API-Key": sync.BRIDGE_API_KEY} if sync.BRIDGE_API_KEY else {}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        try:
            r = await client.get(f"http://{row['host']}:{row['port']}/trades",
                                 params={"days": days})
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"bridge unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code,
                            detail=r.json().get("detail", "bridge error"))
    return r.json()


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
    old = await pool.fetchrow(
        "SELECT mt5_login, mt5_server FROM mt5_hosts WHERE id=$1", host_id)
    await _claim_account(pool, host_id, req.login, req.server)  # 撞号在这里就被数据库拦下, 不碰 bridge
    headers = {"X-API-Key": sync.BRIDGE_API_KEY} if sync.BRIDGE_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            try:
                r = await client.post(
                    f"http://{row['host']}:{row['port']}/connect", json=req.model_dump())
            except httpx.HTTPError as e:
                raise HTTPException(status_code=502, detail=f"bridge unreachable: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.json().get("detail"))
    except HTTPException:
        # 没登上: 退回原账户占位, 不占着一个实际没在用的号
        await pool.execute("UPDATE mt5_hosts SET mt5_login=$2, mt5_server=$3 WHERE id=$1",
                           host_id, old["mt5_login"], old["mt5_server"])
        raise
    await sync.log_host_event(pool, host_id, "ACCOUNT_SET",
                              {"login": req.login, "server": req.server})
    return r.json()
