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

ENV_NAME = os.getenv("ENV_NAME", "dev")
DATABASE_URL = (
    f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}"
    f"@{os.getenv('DB_HOST', 'postgres')}:{os.getenv('DB_PORT', '5432')}"
    f"/{os.getenv('POSTGRES_DB')}"
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

    heartbeat = asyncio.create_task(sync.heartbeat_loop(app.state.pool))
    yield
    heartbeat.cancel()
    await app.state.pool.close()


app = FastAPI(title="MT5 Factory", version="2.0.0", lifespan=lifespan)
app.include_router(strategies.router)


DB_URL_MASKED = (
    f"postgresql://{os.getenv('POSTGRES_USER')}:***"
    f"@{os.getenv('DB_HOST', 'postgres')}:{os.getenv('DB_PORT', '5432')}"
    f"/{os.getenv('POSTGRES_DB')}"
)


@app.get("/health")
async def health(response: Response):
    db_status = 200
    hosts = []
    try:
        rows = await app.state.pool.fetch(
            "SELECT name, host, port,"
            "       COALESCE(last_heartbeat > now() - interval '90 seconds', false) AS online"
            "  FROM mt5_hosts WHERE enabled ORDER BY id")
        hosts = [{"name": r["name"], "host": f"{r['host']}:{r['port']}",
                  "status": "online" if r["online"] else "offline"} for r in rows]
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
VALID_ROLES = {"download", "backtest", "live"}


@app.get("/hosts")
async def list_hosts():
    rows = await app.state.pool.fetch(
        "SELECT id, name, host, port, roles, account_type, enabled, last_heartbeat,"
        "       (last_heartbeat > now() - interval '90 seconds') AS online"
        "  FROM mt5_hosts ORDER BY id"
    )
    return {"hosts": [dict(r) for r in rows]}


class HostCreate(BaseModel):
    name: str
    host: str
    port: int = 9090
    roles: list[str] = ["download"]
    account_type: str = "DEMO"


@app.post("/hosts")
async def create_host(req: HostCreate):
    """注册 worker (等价于往 mt5_hosts 插一行)"""
    if not set(req.roles) <= VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"roles must be subset of {sorted(VALID_ROLES)}")
    if req.account_type not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="account_type must be DEMO or REAL")
    try:
        row = await app.state.pool.fetchrow(
            "INSERT INTO mt5_hosts (name, host, port, roles, account_type)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING *",
            req.name, req.host, req.port, req.roles, req.account_type)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="name or host:port already registered")
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
    if "roles" in fields and not set(fields["roles"]) <= VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"roles must be subset of {sorted(VALID_ROLES)}")
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    row = await app.state.pool.fetchrow(
        f"UPDATE mt5_hosts SET {sets} WHERE id = $1 RETURNING *", host_id, *fields.values())
    if row is None:
        raise HTTPException(status_code=404, detail="host not found")
    return dict(row)


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
