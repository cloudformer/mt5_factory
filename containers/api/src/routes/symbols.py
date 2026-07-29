"""/symbols — 品种主档 (唯一数据源)

一切品种信息只此一处: 下载哪些(download)、每品种起始日期(data_start)、
精度(digits/point)、下单约束(volume_min/stops_level)。下载/回测/策略生成全部只读本表。

关键纪律: 登记品种必须经券商校验, 精度由券商自动带回, 不手填 —
根治"手填 point 靠猜 / 加了券商没有的品种"这类 bug。
校验是异步的(2026-07-26, v7.2 单向化 #7): 登记先入库(待校验), 下载 worker 的 announce
应答里领任务、查本机 MT5、下次 announce 捎回结果(见 hosts.announce_host) —
api 不再反向连 worker; 页面 1~2 分钟后刷出"已校验/失败"。
"""
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


def _parse_date(s: str) -> date:
    """'YYYY-MM-DD' → date 对象。asyncpg 的 date 参数只吃 date 对象不吃字符串,
    必须在这里解析(顺带校验格式, 错的给明确 400 而非 500)"""
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"起始日期格式应为 YYYY-MM-DD: {s!r}")

logger = logging.getLogger("symbols")
router = APIRouter()


@router.get("/symbols")
async def list_symbols(request: Request, coverage: bool = False):
    """全部已登记品种(下载页/策略生成/回测页/Regime 页都读这里)。
    coverage=1 附每品种×每周期数据覆盖 — 只有下载页/首页要它(2026-07-29 性能修复:
    覆盖统计要对整张 historical_bars 聚合, 千万行级后每次几秒; 之前所有页面都在付这笔钱,
    数据爆量后全站变慢的根因)。
    orphans: historical_bars 里有数据、但 symbols 表没登记的品种 —
    直接暴露出来防"看不到的藏数据", 页面可一键清空。"""
    pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT s.symbol, s.broker, s.digits, s.point, s.volume_min, s.stops_level,"
        "       s.download, s.data_start, s.verified_at, s.verify_error"
        "  FROM symbols s ORDER BY s.symbol")
    # 每品种×每周期覆盖: 一条 GROUP BY 全拿, 键=周期 — 下载什么层页面就显示什么层。
    # cov 结构: {tf: {first_bar,last_bar,bars}}
    cov: dict = {}
    if coverage:
        for c in await pool.fetch(
                "SELECT symbol, timeframe, min(time) AS first_bar, max(time) AS last_bar,"
                "       count(*) AS bars FROM historical_bars GROUP BY 1, 2"):
            cov.setdefault(c["symbol"], {})[c["timeframe"]] = {
                "first_bar": c["first_bar"], "last_bar": c["last_bar"], "bars": c["bars"]}
    out = []
    for r in rows:
        d = dict(r)
        d["cov"] = cov.get(r["symbol"], {})
        m1 = d["cov"].get("M1", {})   # 老字段保留(消费者零改动); 无 coverage 时为 None
        d["first_bar"], d["last_bar"], d["bars"] = (
            m1.get("first_bar"), m1.get("last_bar"), m1.get("bars"))
        out.append(d)
    orphans = []
    if coverage:   # 孤儿检查同样是全表聚合, 只有下载页显示它 — 同门控(2026-07-29 性能修复)
        orphans = await pool.fetch(
            "SELECT symbol, min(time) AS first_bar, max(time) AS last_bar, count(*) AS bars"
            "  FROM historical_bars"
            " WHERE symbol NOT IN (SELECT symbol FROM symbols)"
            " GROUP BY symbol ORDER BY symbol")
    return {"symbols": out, "orphans": [dict(r) for r in orphans]}


class SymbolRegister(BaseModel):
    symbol: str
    data_start: str = "2015-01-01"


@router.post("/symbols")
async def register_symbol(req: SymbolRegister, request: Request):
    """登记/重新校验一个品种(异步): 先入库标"待校验", 下载 worker 经 announce 领任务
    查券商, 1~2 分钟后精度自动补齐(或标失败原因)。已存在的品种 = 触发重新校验,
    原精度保留到新结果到达(校验期间不影响已有回测/下载判定)。"""
    name = req.symbol.strip().upper()
    if not name:
        raise HTTPException(status_code=400, detail="symbol 不能为空")
    ds = _parse_date(req.data_start)
    worker = await request.app.state.pool.fetchval(
        "SELECT count(*) FROM mt5_hosts WHERE enabled AND download")
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO symbols (symbol, data_start, download)"
        " VALUES ($1, $2, FALSE)"   # 新品种校验通过才开下载
        " ON CONFLICT (symbol) DO UPDATE SET verified_at=NULL, verify_error=NULL"
        " RETURNING *", name, ds)
    logger.info("symbol %s queued for broker verify (download workers=%d)", name, worker)
    return {**dict(row), "pending": True,
            "hint": ("已登记, 等下载 worker 校验(约1~2分钟, 刷新本页看结果)" if worker
                     else "已登记, 但当前没有启用的下载 worker — worker 上线后自动校验")}


class SymbolUpdate(BaseModel):
    download: bool | None = None
    data_start: str | None = None


@router.patch("/symbols/{symbol}")
async def update_symbol(symbol: str, req: SymbolUpdate, request: Request):
    """改品种的下载开关 / 起始日期 (精度不可手改, 只能靠 POST 重新校验)"""
    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    if "data_start" in fields:  # asyncpg 的 date 参数只吃 date 对象, 字符串会报错
        fields["data_start"] = _parse_date(fields["data_start"])
    sets, args = [], [symbol.upper()]
    for k, v in fields.items():
        args.append(v)
        sets.append(f"{k} = ${len(args)}")
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
