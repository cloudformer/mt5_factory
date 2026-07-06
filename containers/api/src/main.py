"""MT5 Factory - App 服务入口"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from src import strategies, sync

# 配置只在一处: 必须由 docker-compose.yml 注入, 代码不留兜底值, 缺了立刻报错
_missing = [k for k in ("DB_HOST", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
            if not os.getenv(k)]
if _missing:
    raise RuntimeError(f"missing env: {', '.join(_missing)} — 应由 docker-compose.yml 注入")

ENV_NAME = os.getenv("ENV_NAME", "dev")
DB_PORT = os.getenv("DB_PORT", "5432")  # 5432 是 postgres 协议标准端口, 允许默认
DATABASE_URL = (
    f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{DB_PORT}/{os.environ['POSTGRES_DB']}"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}',
)
logger = logging.getLogger("app")


async def _init_conn(conn):
    """jsonb 直接映射 dict, 避免全链路手工 json.dumps/loads"""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for attempt in range(1, 6):
        try:
            app.state.pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=2, max_size=10, init=_init_conn)
            logger.info("Database pool ready")
            break
        except Exception as e:
            logger.warning("DB connect attempt %d failed: %s", attempt, e)
            if attempt == 5:
                raise
            await asyncio.sleep(3)

    # env 的 MT5_HOSTS 仅作首次引导: 表为空时种入, 之后完全由 web/API 管理 (避免删掉的复活)
    if await app.state.pool.fetchval("SELECT count(*) FROM mt5_hosts") == 0:
        mt5_port = int(os.getenv("MT5_PORT", "8020"))
        for host in [h.strip() for h in os.getenv("MT5_HOSTS", "").split(",") if h.strip()]:
            name = f"win-{host.replace('.', '-')}"
            await app.state.pool.execute(
                "INSERT INTO mt5_hosts (name, host, port, roles)"
                " VALUES ($1, $2, $3, '{download,demo}')",
                name, host, mt5_port)
            logger.info("MT5 worker seeded: %s (%s:%s)", name, host, mt5_port)

    heartbeat = asyncio.create_task(sync.heartbeat_loop(app.state.pool))
    yield
    heartbeat.cancel()
    await app.state.pool.close()


app = FastAPI(title="MT5 Factory", version="2.0.0", lifespan=lifespan)
app.include_router(strategies.router)


DB_URL_MASKED = (
    f"postgresql://{os.environ['POSTGRES_USER']}:***"
    f"@{os.environ['DB_HOST']}:{DB_PORT}/{os.environ['POSTGRES_DB']}"
)


@app.get("/health")
async def health(response: Response):
    db_status = 200
    hosts = []
    try:
        rows = await app.state.pool.fetch(
            "SELECT name, host, port, status FROM mt5_hosts WHERE enabled ORDER BY id")
        hosts = [{"name": r["name"], "host": f"{r['host']}:{r['port']}",
                  "status": r["status"].lower()} for r in rows]
    except Exception:
        db_status = 500
        response.status_code = 503
    return {
        "status": "healthy" if db_status == 200 else "degraded",
        "env": ENV_NAME,
        "db": {"url": DB_URL_MASKED, "status": db_status},
        "hosts": hosts,
    }


# ========== Worker ==========
VALID_ROLES = {"download", "demo", "live"}


def _validate_roles(roles: list[str]):
    if not set(roles) <= VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"roles must be subset of {sorted(VALID_ROLES)}")
    if {"demo", "live"} <= set(roles):
        raise HTTPException(status_code=400,
                            detail="同一台主机不能同时担任 demo 和 live (一个MT5终端只能登录一个账户)")


@app.get("/hosts")
async def list_hosts():
    rows = await app.state.pool.fetch(
        "SELECT id, name, host, port, roles, account_type, enabled, status,"
        "       created_at, online_at, offline_at, last_heartbeat"
        "  FROM mt5_hosts ORDER BY id"
    )
    return {"hosts": [dict(r) for r in rows]}


@app.get("/hosts/{host_id}/events")
async def host_events(host_id: int, limit: int = 100):
    """worker 生命周期历史 (注册/上下线/启停/角色变更/账户下发)"""
    rows = await app.state.pool.fetch(
        "SELECT event, detail, created_at FROM mt5_host_events"
        " WHERE host_id=$1 ORDER BY created_at DESC LIMIT $2", host_id, limit)
    return {"events": [dict(r) for r in rows]}


class HostCreate(BaseModel):
    name: str
    host: str
    port: int = 8020
    roles: list[str] = ["download"]
    account_type: str = "DEMO"


@app.post("/hosts")
async def create_host(req: HostCreate):
    """注册 worker (等价于往 mt5_hosts 插一行)"""
    _validate_roles(req.roles)
    if req.account_type not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="account_type must be DEMO or REAL")
    try:
        row = await app.state.pool.fetchrow(
            "INSERT INTO mt5_hosts (name, host, port, roles, account_type)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING *",
            req.name, req.host, req.port, req.roles, req.account_type)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="name or host:port already registered")
    await sync.log_host_event(app.state.pool, row["id"], "REGISTERED", {"source": "manual"})
    return dict(row)


class HostUpdate(BaseModel):
    enabled: bool | None = None
    roles: list[str] | None = None
    host: str | None = None
    port: int | None = None
    account_type: str | None = None


@app.patch("/hosts/{host_id}")
async def update_host(host_id: int, req: HostUpdate):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if "roles" in fields:
        _validate_roles(fields["roles"])
    old = await app.state.pool.fetchrow(
        "SELECT enabled, roles FROM mt5_hosts WHERE id=$1", host_id)
    if old is None:
        raise HTTPException(status_code=404, detail="host not found")
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    row = await app.state.pool.fetchrow(
        f"UPDATE mt5_hosts SET {sets} WHERE id = $1 RETURNING *", host_id, *fields.values())
    if "enabled" in fields and fields["enabled"] != old["enabled"]:
        await sync.log_host_event(app.state.pool, host_id,
                                  "ENABLED" if fields["enabled"] else "DISABLED")
    if "roles" in fields and set(fields["roles"]) != set(old["roles"]):
        await sync.log_host_event(app.state.pool, host_id, "ROLES_CHANGED",
                                  {"from": list(old["roles"]), "to": fields["roles"]})
    return dict(row)


class AnnounceRequest(BaseModel):
    name: str
    host: str
    port: int = 8020


@app.post("/hosts/announce")
async def announce_host(req: AnnounceRequest):
    """worker 自动注册: bridge 启动后周期性自报家门。
    新 worker 以 download 角色入册 (demo/live 必须由人在 web 上指派);
    已存在则只刷新心跳, 不覆盖人工配置。"""
    row = await app.state.pool.fetchrow(
        "INSERT INTO mt5_hosts (name, host, port, roles, last_heartbeat)"
        " VALUES ($1, $2, $3, '{download}', now())"
        " ON CONFLICT (host, port) DO UPDATE SET last_heartbeat = now()"
        " RETURNING id, name, roles, enabled, (xmax = 0) AS inserted",
        req.name, req.host, req.port)
    if row["inserted"]:
        await sync.log_host_event(app.state.pool, row["id"], "REGISTERED", {"source": "announce"})
    return {k: row[k] for k in ("id", "name", "roles", "enabled")}


@app.delete("/hosts/{host_id}")
async def delete_host(host_id: int):
    row = await app.state.pool.fetchrow(
        "DELETE FROM mt5_hosts WHERE id=$1 RETURNING name", host_id)
    if row is None:
        raise HTTPException(status_code=404, detail="host not found")
    return {"deleted": row["name"]}


# ========== 系统配置 (下载品种/起始日期等) ==========
CONFIG_KEYS = {"symbols", "data_start"}


@app.get("/config")
async def get_config():
    rows = await app.state.pool.fetch("SELECT key, value, updated_at FROM config ORDER BY key")
    return {"config": {r["key"]: r["value"] for r in rows}}


class ConfigUpdate(BaseModel):
    value: object


@app.put("/config/{key}")
async def set_config(key: str, req: ConfigUpdate):
    if key not in CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"unknown key, allowed: {sorted(CONFIG_KEYS)}")
    if key == "symbols":
        if not isinstance(req.value, list) or not all(isinstance(s, str) and s for s in req.value):
            raise HTTPException(status_code=400, detail="symbols must be a list of strings")
        req.value = [s.strip().upper() for s in req.value]
    if key == "data_start":
        try:
            from datetime import date
            date.fromisoformat(str(req.value))
        except ValueError:
            raise HTTPException(status_code=400, detail="data_start must be YYYY-MM-DD")
    await app.state.pool.execute(
        "INSERT INTO config (key, value) VALUES ($1, $2)"
        " ON CONFLICT (key) DO UPDATE SET value = $2", key, req.value)
    return {key: req.value}


class ConnectRequest(BaseModel):
    login: int
    password: str
    server: str


@app.post("/hosts/{host_id}/connect")
async def connect_host(host_id: int, req: ConnectRequest):
    """向 worker 远程下发 MT5 账户 (免去在 Windows 上手动配置)"""
    row = await app.state.pool.fetchrow(
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
    await app.state.pool.execute(
        "UPDATE mt5_hosts SET mt5_login=$2, mt5_server=$3 WHERE id=$1",
        host_id, req.login, req.server)
    await sync.log_host_event(app.state.pool, host_id, "ACCOUNT_SET",
                              {"login": req.login, "server": req.server})
    return r.json()


# ========== 数据同步 (统一 /syncdata 前缀) ==========
@app.post("/syncdata")
async def start_sync():
    """触发全量/增量数据同步 (断点续传, 后台执行)"""
    if sync.state["running"]:
        raise HTTPException(status_code=409, detail="sync already running")
    sync.state["running"] = True
    asyncio.create_task(sync.run_full_sync(app.state.pool))
    return {"started": True}


@app.get("/syncdata/status")
async def sync_status():
    return sync.state


@app.get("/syncdata/coverage")
async def data_coverage():
    """每个品种已入库的数据范围, 验证下载结果用"""
    rows = await app.state.pool.fetch(
        "SELECT symbol, min(time) AS first_bar, max(time) AS last_bar, count(*) AS bars"
        "  FROM historical_bars WHERE timeframe='M1' GROUP BY symbol ORDER BY symbol"
    )
    return {"coverage": [dict(r) for r in rows]}
