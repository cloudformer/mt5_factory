"""/hosts — Windows worker 管理

职责: worker 注册(手动/自动上报)、启停、删除、职能设置、事件历史、远程下发 MT5 账户。
状态(ONLINE/OFFLINE)由 services.sync.heartbeat_loop 维护, 这里只读写注册信息。

职能模型 (约束靠数据库结构): download BOOLEAN 是否下载;
runner = demo|live|NULL 跑什么策略 — 单字段天然保证 demo/live 互斥。
"""
import hashlib
import logging
import time

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import identity, sync

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


# 心跳到达间隔滑动窗(2026-08-04 Frank 定, worker 体检"节奏读数"): api 内存记最近 11 次
# 到达时刻 → 10 个间隔的 avg/max。期望≈30s, 漂移=网络堵或 worker 卡(圈拖长挤占心跳线程)。
# 内存即可(观察用, api 重启从头累计) — 与下载进度同一裁定, 不 jobs 化不落库
_hb_arrivals: dict = {}


@router.get("/hosts")
async def list_hosts(request: Request):
    uid = identity.scope_uid(request)   # v5.6 通电: 非 owner 只见自己的 worker
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, host, port, download, runner, account_type, enabled, status,"
        "       owner_id, mt5_login, mt5_server,"   # 归属与账户: 管理页划拨下拉/账户列要显示
        "       created_at, online_at, offline_at, last_heartbeat, last_health,"
        # 认证状态(2026-08-02 Frank 定): 有有效 worker 钥匙绑定=已认证;
        # 克隆机复用旧钥不会绑上(一机一钥) → false → 页面红字提示 + 禁指派角色
        "       EXISTS(SELECT 1 FROM worker_keys wk"
        "               WHERE wk.host_id = mt5_hosts.id AND wk.enabled) AS key_bound,"
        # 钥匙名片(2026-08-04 Frank 定): 绑定钥匙的备注+编号, license 行显示"哪把钥匙"
        # (明文只存 sha256 不可逆, 后4位显示不了 — 备注名就是钥匙的人话身份)
        "       (SELECT concat('#', wk.id, coalesce(' ' || wk.name, ''))"
        "          FROM worker_keys wk WHERE wk.host_id = mt5_hosts.id AND wk.enabled"
        "         LIMIT 1) AS key_label,"
        # 库侧生效挂载数(两把钥匙口径: 挂载启用 + 策略状态==本机角色) — 与 runner 实际
        # 认领数对照, 不一致=认领异常(体检"天然异常"项, 不用学习基线)
        "       (SELECT count(*) FROM strategy_mounts sm JOIN strategies s"
        "           ON s.id = sm.strategy_id"
        "         WHERE sm.host_id = mt5_hosts.id AND sm.enabled"
        "           AND s.status = upper(coalesce(mt5_hosts.runner, ''))) AS mounts_active"
        "  FROM mt5_hosts" + (" WHERE owner_id = $1" if uid else "") + " ORDER BY id",
        *([uid] if uid else []))
    hosts = []
    for r in rows:
        d = dict(r)
        arr = _hb_arrivals.get(d["name"]) or []
        gaps = [b - a for a, b in zip(arr, arr[1:])]
        if gaps:
            d["hb_gaps"] = {"n": len(gaps), "avg": round(sum(gaps) / len(gaps), 1),
                            "max": round(max(gaps), 1)}
        hosts.append(d)
    return {"hosts": hosts}


@router.get("/hosts/{host_id}/events")
async def host_events(host_id: int, request: Request, limit: int = 100):
    """worker 生命周期历史 (注册/上下线/启停/角色变更/账户下发)"""
    await identity.assert_host_visible(request.app.state.pool, request, host_id)
    rows = await request.app.state.pool.fetch(
        "SELECT event, detail, created_at FROM mt5_host_events"
        " WHERE host_id=$1 ORDER BY created_at DESC LIMIT $2", host_id, limit)
    return {"events": [dict(r) for r in rows]}


# worker 归属没有手动划拨端点(2026-07-25 与 Frank 定): 归属唯一真相 = 绑定钥匙的主人,
# announce 每分钟自动对齐(手动改会被钥匙改回, 且会造成钥匙与归属两套事实)。
# 换归属 = 吊销旧钥匙 + 给新用户发钥匙 + 机器换钥。


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
    key: str | None = None   # worker 钥匙(schema/040): 带钥=自动归钥主+首绑; 不带=照旧
    # 品种校验结果回传(v7.2 单向化 #7): {品种名: {digits,point,volume_min,stops_level,broker}
    # 或 {"error": 原因}} — 上轮 announce 应答里 verify_symbols 派的任务, 这轮捎回
    symbol_info: dict | None = None


@router.post("/hosts/announce")
async def announce_host(req: AnnounceRequest, request: Request):
    """worker 自动注册: bridge 启动后周期性自报家门。
    身份 = name(计算机名); IP:port 只是当前地址, 每次刷新 —— DHCP/换网都不影响身份。
    新 worker 默认只承担下载 (runner 必须由人指派); 已存在则刷新地址+心跳, 不覆盖人工配置。
    带 worker 钥匙(env WORKER_KEY)时: 认钥知主 → host 归该用户 + 钥匙与本机首绑(一机一钥);
    不带钥照旧(兼容期; 强制认钥在 v5.6)。"""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "INSERT INTO mt5_hosts (name, host, port, download, last_heartbeat)"
        " VALUES ($1, $2, $3, TRUE, now())"
        " ON CONFLICT (name) DO UPDATE SET host = $2, port = $3, last_heartbeat = now()"
        " RETURNING id, name, download, runner, enabled, owner_id, (xmax = 0) AS inserted",
        req.name, req.host, req.port)
    if row["inserted"]:
        await sync.log_host_event(pool, row["id"], "REGISTERED", {"source": "announce"})
    key_state = None
    if req.key:
        wk = await pool.fetchrow(
            "SELECT id, user_id, host_id FROM worker_keys"
            " WHERE key_hash=$1 AND enabled", hashlib.sha256(req.key.encode()).hexdigest())
        if wk is None:
            key_state = "invalid"          # 兼容期只记不拒(v5.6 起拒绝)
            logger.warning("announce %s with invalid/revoked worker key", req.name)
        elif wk["host_id"] in (None, row["id"]):
            try:
                await pool.execute(   # 首绑(或重复确认) + host 归钥主; last_used 顺带刷新
                    "UPDATE worker_keys SET host_id=$2, last_used_at=now() WHERE id=$1",
                    wk["id"], row["id"])
                await pool.execute(
                    "UPDATE mt5_hosts SET owner_id=$2 WHERE id=$1 AND owner_id <> $2",
                    row["id"], wk["user_id"])
                key_state = "bound"
                if wk["host_id"] is None:
                    await sync.log_host_event(pool, row["id"], "KEY_BOUND",
                                              {"worker_key_id": wk["id"], "owner": wk["user_id"]})
            except asyncpg.UniqueViolationError:   # 本机已被另一把钥匙绑定
                key_state = "conflict"
                logger.warning("announce %s: host already bound to another worker key", req.name)
        else:
            key_state = "conflict"         # 这把钥匙已绑别的机器(克隆机忘换钥匙的典型)
            logger.warning("announce %s: worker key already bound to host #%s",
                           req.name, wk["host_id"])
    # 品种校验收发(v7.2 单向化 #7, 仅下载职能的启用机参与):
    # ①收: 上轮派的任务这轮捎回结果 → 补齐精度(只补待校验行, 防旧结果覆盖已校验数据)
    if req.symbol_info and row["download"] and row["enabled"]:
        for sym, info in req.symbol_info.items():
            if not isinstance(info, dict):
                continue
            sym = str(sym).strip().upper()
            if info.get("error"):
                await pool.execute(
                    "UPDATE symbols SET verify_error=$2"
                    " WHERE symbol=$1 AND verified_at IS NULL",
                    sym, str(info["error"])[:200])
            elif info.get("point"):
                # v2.3 户口制: broker 是行身份的一半, 不再被校验回写覆盖 —
                # 回写只认"同券商"的登记行(worker 报的 broker 与行不符 = 别家的行, 不碰;
                # 老 worker 不报 broker($6=NULL)时按品种名放行, 单券商语义不变)
                await pool.execute(
                    "UPDATE symbols SET digits=$2, point=$3, volume_min=$4, stops_level=$5,"
                    "       download=TRUE, verified_at=now(), verify_error=NULL"
                    " WHERE symbol=$1 AND verified_at IS NULL"
                    "   AND ($6::varchar IS NULL OR broker=$6)",
                    sym, info.get("digits"), info["point"], info.get("volume_min"),
                    info.get("stops_level"), info.get("broker"))
                logger.info("symbol %s verified via %s (point=%s)", sym, req.name, info["point"])
    out = {k: row[k] for k in ("id", "name", "download", "runner", "enabled")}
    # worker 参数下发(config 唯一源, schema/046): 报到即领最新, 配置页改完 1~2 分钟生效
    params = await pool.fetchval("SELECT value FROM config WHERE key='worker_params'")
    if params:
        out["params"] = params
    # ②派: 待校验且未标失败的品种 → 应答里带任务, bridge 查 MT5 下轮捎回
    if row["download"] and row["enabled"]:
        pend = await pool.fetch(
            "SELECT symbol FROM symbols"
            " WHERE verified_at IS NULL AND verify_error IS NULL LIMIT 10")
        if pend:
            out["verify_symbols"] = [r["symbol"] for r in pend]
    if key_state:
        out["key_state"] = key_state
    return out


@router.post("/hosts/heartbeat")
async def push_heartbeat(request: Request):
    """v7.2 一期(2026-07-26 与 Frank 定): worker 主动推心跳 — bridge 每 30s POST,
    payload = 其 /health 同一份数据 + name(计算机名, 身份与 announce 一致)。
    与轮询共用同一收货函数(sync.ingest_health); 轮询侧看到 75s 内的新鲜推送即跳过
    反向探测(双栈过渡, 推送停了自动回轮询)。trades 仍走拉取(#2 未迁)。"""
    try:
        health = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body 必须是 JSON")
    name = str(health.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺 name(计算机名)")
    pool = request.app.state.pool
    h = await pool.fetchrow(
        "SELECT id, name, status, runner FROM mt5_hosts WHERE name=$1 AND enabled", name)
    if h is None:  # 未注册: 等 announce(每分钟)先建档, 下一拍推送即被接受
        raise HTTPException(status_code=404, detail="host 未注册或已停用 — announce 会自动建档")
    # 到达间隔滑动窗(体检"节奏读数"): 只记时刻, 统计在 /hosts 现算
    arr = _hb_arrivals.setdefault(name, [])
    arr.append(time.time())
    del arr[:-11]
    health["push_v"] = 1   # 服务端强制打标(轮询跳过的依据), 不信任 payload 自带
    health.pop("trades_v", None)
    resp = {"accepted": True}
    # 成交捎带(v7.2 #2): 单独取出入库, 不进 last_health(防每30s存一大块JSON)。
    # 入库成功才打 trades_v 标(轮询凭它免拉); 失败不打标+回明确原因 → 轮询自动回退拉取, 数据不丢
    trades = health.pop("trades", None)
    login = health.get("login")
    if h["runner"] and login:
        if isinstance(trades, list):
            try:
                stored, bad = await sync.ingest_trades(
                    pool, h, int(login), trades, health.get("server"))
                health["trades_v"] = 1
                resp["trades_stored"], resp["trades_bad"] = stored, bad
            except Exception as e:   # 库故障/数据形状意外: 原因回给 worker 日志, 本拍不打标
                logger.error("heartbeat trades ingest %s(acct %s) failed: %s: %s",
                             name, login, type(e).__name__, e)
                resp["trades_error"] = f"{type(e).__name__}: {e}"
        # 下一拍成交窗口(自适应: 空库回填90天, 稳态3天) — 刚入完库现算, 窗口即时收缩
        resp["trades_days"] = await sync.trades_window_days(pool, int(login))
    await sync.ingest_health(pool, h, health)
    return resp


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
    # 认证执法(2026-08-02 Frank 定): 没有有效 worker 钥匙绑定的机器不许进任何池 —
    # 克隆机复用旧钥永远绑不上(一机一钥), 必须签发新钥才能指派角色
    if "runner" in fields and fields["runner"]:
        bound = await pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM worker_keys WHERE host_id=$1 AND enabled)", host_id)
        if not bound:
            raise HTTPException(
                status_code=400,
                detail="该 worker 未认证(无有效钥匙绑定) — 去「用户管理」页签发新 worker 钥匙,"
                       " 填入该机 env/.dev.env 的 WORKER_KEY 并重启 bridge, 绑定后再指派角色")
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


# 远程重启端点已删(2026-07-26 与 Frank 定, v7.2 单向化取舍): api 不再主动连 worker。
# bridge 本地 /restart 与看门狗保留 — 真要重启上机操作; 未来需要再以"worker 轮询待办"回归。


# /hosts/{id}/trades 实时透传端点已删(2026-07-26 v7.2 收口): 流水页改读
# 心跳快照(last_health.positions) + 库内回合(trades 表); MT5 原始 deal 腿备用入口 =
# worker 本机 :8020/trades?fmt=html。至此 api 对 worker 零出站。


# 远程下发 MT5 账户端点已删(2026-07-26 与 Frank 定, v7.2 单向化取舍): 账户本来就是
# 部署时写在机器 env 的事, announce/心跳会回报实际登录账号(_claim_account 撞号执法保留)。
