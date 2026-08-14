"""api 入口 — 应用装配 + 健康检查

职责: 创建 FastAPI 应用、数据库连接池、后台任务(心跳)、挂载全部路由。
业务端点不写在这里 — 按领域放在 routes/ 下, 业务逻辑放在 services/ 下。

扩展点: 加新一组 API = routes/ 下新建文件 + 在 routes/__init__.py 注册。
"""
import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Response

from src.routes import ROUTERS
from src.services import jobs, sync

# ---- 配置只在一处: 必须由 docker-compose.yml 注入, 代码不留兜底值 ----
_missing = [k for k in ("DB_HOST", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
            if not os.getenv(k)]
if _missing:
    raise RuntimeError(f"missing env: {', '.join(_missing)} — 应由 docker-compose.yml 注入")

ENV_NAME = os.getenv("ENV_NAME", "dev")
DB_PORT = os.getenv("DB_PORT", "5432")
DATABASE_URL = (
    f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{DB_PORT}/{os.environ['POSTGRES_DB']}"
)
DB_URL_MASKED = (
    f"postgresql://{os.environ['POSTGRES_USER']}:***"
    f"@{os.environ['DB_HOST']}:{DB_PORT}/{os.environ['POSTGRES_DB']}"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}',
)
logger = logging.getLogger("api")


async def _init_conn(conn):
    """jsonb 直接映射 dict, 避免全链路手工 json.dumps/loads"""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 数据库连接池 (带重试, 等待 postgres healthcheck 就绪)
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

    # schema 自动对齐 (唯一机制): containers/postgres/schema/ 按文件名顺序全部执行。
    # 文件全幂等 — 空库建全量, 老库无害跳过; 失败即启动失败, 绝不带着错的结构运行。
    schema_dir = Path(__file__).resolve().parent.parent / "schema"
    schema_files = sorted(schema_dir.glob("*.sql"))
    if not schema_files:  # 挂载丢了必须炸而不是静默跳过 — 否则空库会以"无表"状态运行
        raise RuntimeError(f"no schema files in {schema_dir} — compose 应挂载 containers/postgres/schema")
    # advisory lock 串行化(铁律6): 多副本同时启动时只有一个执行建表, 其余等它完成后再跑
    # (幂等无害)。锁随连接释放, 不会悬挂。
    async with app.state.pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(714003)")
        try:
            for f in schema_files:
                try:
                    await conn.execute(f.read_text())
                    logger.info("schema applied: %s", f.name)
                except Exception as e:
                    raise RuntimeError(f"schema {f.name} failed: {e}") from e
        finally:
            await conn.execute("SELECT pg_advisory_unlock(714003)")

    # worker 建档只有一条路(v7.2 单向化): bridge 的 announce 自己上报 —
    # api 认钥知主、一机一钥绑定都在那条路上。原 MT5_HOSTS 首次引导种子已删(2026-08-13):
    # 种出来的记录没 owner_id、没绑钥匙, 页面判"未认证"禁指派角色, 种了也不能用;
    # 而且该变量从未进过 env 模板, 那段 for 循环从没执行过。
    heartbeat = asyncio.create_task(sync.heartbeat_loop(app.state.pool))
    # jobs 消费者(schema/020): 批量回测的执行体 — SKIP LOCKED 抢单, 多副本安全,
    # 启动即消费(api 重启后接着跑上次没跑完的批次)
    consumer = asyncio.create_task(jobs.consumer_loop(app.state.pool))
    yield
    heartbeat.cancel()
    consumer.cancel()
    await app.state.pool.close()


app = FastAPI(title="MT5 Factory", version="2.0.0", lifespan=lifespan)
for router in ROUTERS:
    app.include_router(router)


@app.middleware("http")
async def identify_caller(request, call_next):
    """身份识别(v5.6-A 最小版, 2026-07-25 与 Frank 定: 只识别不限制) —
    带 X-API-Key(用户钥匙或 worker 钥匙)则解析出"是谁"挂到 request.state 并记日志;
    不带/无效照常放行, 对任何请求零拦截。
    2026-07-26 通电(读路径过滤): 无钥匙时接受 web 转发的 X-User-Id(右上角切换的身份,
    登录前过渡、信任内网 web) — 列表类读端点按 services.identity.scope_uid 过滤资产;
    登录上线后 web 改发真凭据, 本中间件与各端点零改动。401/写路径拦截仍不在这里。"""
    request.state.user_id, request.state.auth_kind = None, None
    key = request.headers.get("X-API-Key")
    dev_uid = request.headers.get("X-User-Id")
    if not key and dev_uid and dev_uid.isdigit():
        request.state.user_id, request.state.auth_kind = int(dev_uid), "dev"
    pool = getattr(app.state, "pool", None)
    if key and pool is not None:
        h = hashlib.sha256(key.encode()).hexdigest()
        row = await pool.fetchrow(
            "SELECT k.id, k.user_id, u.name FROM api_keys k"
            " JOIN users u ON u.id = k.user_id"
            " WHERE k.key_hash=$1 AND k.enabled AND u.enabled", h)
        kind = "user"
        if row is None:
            row = await pool.fetchrow(
                "SELECT k.id, k.user_id, u.name FROM worker_keys k"
                " JOIN users u ON u.id = k.user_id"
                " WHERE k.key_hash=$1 AND k.enabled AND u.enabled", h)
            kind = "worker"
        if row is not None:
            request.state.user_id, request.state.auth_kind = row["user_id"], kind
            await pool.execute(
                f"UPDATE {'api_keys' if kind == 'user' else 'worker_keys'}"
                " SET last_used_at=now() WHERE id=$1", row["id"])
            logger.debug("caller: user #%d %s (%s key) %s %s",
                         row["user_id"], row["name"], kind,
                         request.method, request.url.path)
    return await call_next(request)


def _human_bytes(n) -> str:
    if n is None:
        return "—"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024


async def _storage(pool) -> dict:
    """DB 容量(pg 函数, RDS/docker 通用) + 应用服务器磁盘用量(best-effort)。
    全程 try: 取到就带上, 取不到就不带 — 任何一项失败都不影响 /health。
    磁盘是"api 跑在哪台机器"的盘: 本地 docker 下它就是 DB 数据卷所在盘; RDS 下是应用机的盘。"""
    out = {}
    try:
        out["db_size"] = _human_bytes(await pool.fetchval("SELECT pg_database_size(current_database())"))
    except Exception as e:
        logger.warning("db size query failed: %s", e)
    try:
        import shutil
        du = shutil.disk_usage("/app")
        out["disk"] = f"{_human_bytes(du.used)} / {_human_bytes(du.total)}"
        out["disk_pct"] = round(du.used / du.total * 100, 1)
    except Exception as e:
        logger.warning("disk usage failed: %s", e)
    return out


@app.get("/health")
async def health(response: Response):
    """整体健康: api + db(地址/状态/容量) + 磁盘用量 + 全部 worker 在线状态"""
    db_status = 200
    hosts, storage = [], {}
    try:
        rows = await app.state.pool.fetch(
            "SELECT name, host, port, status FROM mt5_hosts WHERE enabled ORDER BY id")
        hosts = [{"name": r["name"], "host": f"{r['host']}:{r['port']}",
                  "status": r["status"].lower()} for r in rows]
        storage = await _storage(app.state.pool)
    except Exception:
        db_status = 500
        response.status_code = 503
    return {
        "status": "healthy" if db_status == 200 else "degraded",
        "env": ENV_NAME,
        "db": {"url": DB_URL_MASKED, "status": db_status},
        "storage": storage,
        "hosts": hosts,
    }
