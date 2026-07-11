"""/symbols — 品种主档 (唯一数据源)

一切品种信息只此一处: 下载哪些(download)、每品种起始日期(data_start)、
精度(digits/point)、下单约束(volume_min/stops_level)。下载/回测/策略生成全部只读本表。

关键纪律: 登记品种必须经券商校验 (POST /symbols 调 bridge /symbol/{name}),
精度由券商自动带回, 不手填 — 根治"手填 point 靠猜 / 加了券商没有的品种" 这类 bug。
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import httpx

from src.services import sync

logger = logging.getLogger("symbols")
router = APIRouter()


@router.get("/symbols")
async def list_symbols(request: Request):
    """全部已登记品种 + 每品种 M1 数据覆盖 (下载页/策略生成都读这里)。
    orphans: historical_bars 里有数据、但 symbols 表没登记的品种 —
    直接暴露出来防"看不到的藏数据", 页面可一键清空。"""
    pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT s.symbol, s.digits, s.point, s.volume_min, s.stops_level,"
        "       s.download, s.role, s.data_start, s.verified_at,"
        "       c.first_bar, c.last_bar, c.bars"
        "  FROM symbols s"
        "  LEFT JOIN LATERAL (SELECT min(time) AS first_bar, max(time) AS last_bar,"
        "                            count(*) AS bars FROM historical_bars"
        "                      WHERE symbol = s.symbol AND timeframe='M1') c ON true"
        " ORDER BY s.role, s.symbol")
    orphans = await pool.fetch(
        "SELECT symbol, min(time) AS first_bar, max(time) AS last_bar, count(*) AS bars"
        "  FROM historical_bars"
        " WHERE symbol NOT IN (SELECT symbol FROM symbols)"
        " GROUP BY symbol ORDER BY symbol")
    return {"symbols": [dict(r) for r in rows], "orphans": [dict(r) for r in orphans]}


class SymbolRegister(BaseModel):
    symbol: str
    role: str = "trade"            # trade | validate
    data_start: str = "2015-01-01"


async def _broker_symbol(pool, name: str) -> dict:
    """向任一下载 worker 的券商查这个品种是否存在及其真实精度。
    券商没有 → 400 明确报错 (不再是下载时才炸的 500)。"""
    host = await pool.fetchrow(
        "SELECT host, port FROM mt5_hosts WHERE enabled AND download ORDER BY id LIMIT 1")
    if host is None:
        raise HTTPException(status_code=400, detail="没有可用的下载 worker, 无法向券商校验品种")
    headers = {"X-API-Key": sync.BRIDGE_API_KEY} if sync.BRIDGE_API_KEY else {}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        try:
            r = await client.get(f"http://{host['host']}:{host['port']}/symbol/{name}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"bridge unreachable: {e}")
    if r.status_code == 404:
        raise HTTPException(status_code=400,
                            detail=f"该券商没有品种 {name} — 名称可能不同(如 {name}.m / Bitcoin), "
                                   "在 MT5 报价窗 Ctrl+M 查实际名称")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json().get("detail", "bridge error"))
    return r.json()


@router.post("/symbols")
async def register_symbol(req: SymbolRegister, request: Request):
    """登记一个品种: 向券商校验存在性 + 自动取真实精度/下单约束后入库"""
    if req.role not in ("trade", "validate"):
        raise HTTPException(status_code=400, detail="role must be trade or validate")
    name = req.symbol.strip().upper()
    if not name:
        raise HTTPException(status_code=400, detail="symbol 不能为空")
    info = await _broker_symbol(request.app.state.pool, name)
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO symbols (symbol, digits, point, volume_min, stops_level,"
        "                     role, data_start, download, verified_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7::date, TRUE, now())"
        " ON CONFLICT (symbol) DO UPDATE SET"
        "   digits=$2, point=$3, volume_min=$4, stops_level=$5, verified_at=now()"
        " RETURNING *",
        name, info["digits"], info["point"], info.get("volume_min"),
        info.get("trade_stops_level"), req.role, req.data_start)
    logger.info("symbol registered: %s (digits=%s point=%s)", name, info["digits"], info["point"])
    return dict(row)


class SymbolUpdate(BaseModel):
    download: bool | None = None
    data_start: str | None = None
    role: str | None = None


@router.patch("/symbols/{symbol}")
async def update_symbol(symbol: str, req: SymbolUpdate, request: Request):
    """改品种的下载开关 / 起始日期 / 角色 (精度不可手改, 只能靠 POST 重新校验)"""
    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if "role" in fields and fields["role"] not in ("trade", "validate"):
        raise HTTPException(status_code=400, detail="role must be trade or validate")
    sets, args = [], [symbol.upper()]
    for k, v in fields.items():
        args.append(v)
        sets.append(f"{k} = ${len(args)}" + ("::date" if k == "data_start" else ""))
    row = await request.app.state.pool.fetchrow(
        f"UPDATE symbols SET {', '.join(sets)} WHERE symbol = $1 RETURNING *", *args)
    if row is None:
        raise HTTPException(status_code=404, detail="symbol not found")
    return dict(row)


@router.delete("/symbols/{symbol}/data")
async def purge_symbol_data(symbol: str, request: Request):
    """清空某品种的全部历史 K线 (删登记前必须先做这步; 也用于清理孤儿数据)"""
    name = symbol.upper()
    result = await request.app.state.pool.execute(
        "DELETE FROM historical_bars WHERE symbol=$1", name)
    deleted = int(result.split()[-1])
    logger.info("purged %d bars for %s", deleted, name)
    return {"symbol": name, "deleted_bars": deleted}


@router.delete("/symbols/{symbol}")
async def delete_symbol(symbol: str, request: Request):
    """删除品种登记。铁律: 有历史数据时拒绝 —— 必须先清空数据, 杜绝无登记的孤儿数据"""
    name = symbol.upper()
    bars = await request.app.state.pool.fetchval(
        "SELECT count(*) FROM historical_bars WHERE symbol=$1", name)
    if bars:
        raise HTTPException(
            status_code=409,
            detail=f"{name} 还有 {bars:,} 根历史数据 — 先『清空数据』再删除(避免看不到的孤儿数据)")
    row = await request.app.state.pool.fetchrow(
        "DELETE FROM symbols WHERE symbol=$1 RETURNING symbol", name)
    if row is None:
        raise HTTPException(status_code=404, detail="symbol not found")
    return {"deleted": row["symbol"]}
