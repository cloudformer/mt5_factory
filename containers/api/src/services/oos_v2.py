"""OOS 筛选 v2(v0.6)的口径与判定 — 判定逻辑全系统只有这一份, 两条路共用:

  · 点名诊断: routes/oos_v2.py 内同步现跑回测 → judge_one(只读不入库)
  · 全池清理: worker 并行跑完 jobs 队列(kind=oos_v2) → 本模块 finalize()(第4步接)

口径(2026-08-06 与 Frank 定稿, docs/2.regime_dirction/v0.6_OOS筛选v2设计.md):
  · 锚点 A = 跑批当天 00:00 UTC(全批冻结, 批内可比); 年 = 365.25 天; 笔归段按
    entry_time, 区间左闭右开 [起, 止) — 三条约定由测试锁死, 改动=改口径须重新定版
  · 每策略只跑一次 20 年回测(窗口 = 各段最早起点自动取最大), 六段全部是对同一份
    trades 的时间过滤 + 引擎 _metrics()(尺子同一把, 本模块零撮合逻辑)
  · 段合格 = PF > 该期生效门槛(每期 min_pf, null → default_pf), 或无亏损段(PF=∞ 恒过),
    或 0 笔段(无数据不追责 → 算过, 显示 —); 段不合格 = 有笔且 PF ≤ 门槛(含全亏 PF=0)。
    六段全合格 = PASS, 任一红 = FAIL, 没有第三种结论。
  · 警示与判定分离: 0 笔段 / 笔数 < min_seg_trades 挂「样本不足」只提示人工, 不参与判定

数据安全铁则(照 v0.5, 改这里前先读):
  1. 任务 FAILED / 缺回测行 → 该策略记 skip, 永不归档也不打标签 — 缺数据绝不淘汰, 下次重跑
  2. 归档写入时重申 status='CANDIDATE'(防跑批期间被挂上机器)
  3. 预览模式除【报告 + 出池标签(basis)】外零写入(2026-08-07 Frank 定: 预览也打标签,
     否则 6000 个永远跑不完一轮); 点名诊断恒零写入
  4. 判定是纯读+纯计算; 引擎路径不在这里(worker 的 jobs._run_one 与批量回测同一份)
  5. 收尾完删空本工种队列 = "队列空即无待收尾批次", 幂等自清理(不加表不加列)
"""
import logging
from datetime import date, datetime, timedelta, timezone

import asyncpg

from src.services import backtest, jobs

logger = logging.getLogger("oos_v2")

TAG = "oos_v2"           # basis 标签词根: 「oos_v2#<报告号>」= 已筛过出池 + 报告溯源
LOCK_KEY = 0x005EC42     # advisory lock: 同一时刻只有一个 api 副本在收尾(与 screen 不同键)
YEAR_DAYS = 365.25       # 年 = 365.25 天(与 v0.5 同约定, 测试锁死)


def cfg_params(cfg: dict) -> dict:
    """config.oos_v2 → 判据(校验 + 带默认): 页面保存 / 运行快照 / 收尾判定三处同一口径。
    非法配置直接 ValueError(保存被拒不落库, 运行前置失败不投队列)。"""
    segs = cfg.get("segments")
    if not isinstance(segs, list) or not segs:
        raise ValueError("segments 不能为空")
    out, names = [], set()
    for i, s in enumerate(segs):
        name = str(s.get("name") or "").strip()
        if not name:
            raise ValueError(f"segments[{i}] 缺 name")
        if name in names:
            raise ValueError(f"segments[{i}] name 重复: {name}")
        names.add(name)

        def _span(key):
            v = s.get(key)
            if not isinstance(v, (list, tuple)) or len(v) != 2:
                raise ValueError(f"segments[{i}].{key} 须为 [起, 止](距锚点年数)")
            try:
                a, b = float(v[0]), float(v[1])
            except (TypeError, ValueError):
                raise ValueError(f"segments[{i}].{key} 必须是数字")
            if not (a > b >= 0):
                raise ValueError(
                    f"segments[{i}].{key}: 起({v[0]}) 必须大于 止({v[1]}), 且止 ≥ 0")
            return [a, b]

        mp = s.get("min_pf")
        out.append({"name": name, "label": str(s.get("label") or name),
                    "train": _span("train"), "test": _span("test"),
                    "min_pf": None if mp in (None, "") else float(mp)})
    dp = cfg.get("default_pf")
    rd = cfg.get("reuse_days")   # 复用天数: N天内跑过覆盖全窗的回测就不重跑; 0=每次都现跑
    return {"segments": out,
            "default_pf": float(dp) if dp is not None else 1.0,
            "min_seg_trades": int(cfg.get("min_seg_trades") or 10),
            "batch_limit": int(cfg.get("batch_limit") or 50),
            "reuse_days": int(rd) if rd is not None else 7}


def window_years(p: dict) -> float:
    """回测窗口(年) = 各段最早起点自动取最大 — 不单独配, 免得和段定义打架"""
    return max(x for s in p["segments"] for x in (s["train"][0], s["test"][0]))


def anchor_dt(anchor: date) -> datetime:
    """锚点日期 → 时间戳基准: 当天 00:00 UTC(定版约定, 不纠结小时级误差)"""
    return datetime(anchor.year, anchor.month, anchor.day, tzinfo=timezone.utc)


def seg_window(a: datetime, span: list) -> tuple[float, float]:
    """[起, 止](距锚点年数) → (start_ts, end_ts), 左闭右开 [起, 止)"""
    return ((a - timedelta(days=span[0] * YEAR_DAYS)).timestamp(),
            (a - timedelta(days=span[1] * YEAR_DAYS)).timestamp())


def _seg_stat(trades: list, t0: float, t1: float, eff_pf: float, floor: int) -> dict:
    """一个段的读数 + 合格判定。指标全部来自引擎 _metrics(尺子同一把, 不自算):
    n=0 → pf=None 显示 —, 无数据不追责算过; n>0 且 _metrics pf=None → 无亏损段 ∞ 恒过;
    其余 pf > eff_pf 才过。warn = 笔数 < floor(样本不足, 只提示不判定)。"""
    sub = [t for t in trades if t0 <= t["entry_time"] < t1]
    m = backtest._metrics(sub)
    n = m["trades"]
    pf = m.get("profit_factor")          # 0 笔缺键 → None; 无亏损段引擎也给 None
    ok = True if n == 0 else (True if pf is None else pf > eff_pf)
    return {"n": n, "net": m.get("net_points", 0.0), "pf": pf,
            "inf": bool(n and pf is None),           # ∞ 与 0 笔的 — 由此区分
            "dd": m.get("max_dd_points"), "ok": ok, "warn": n < floor,
            "from": f"{datetime.fromtimestamp(t0, tz=timezone.utc):%Y-%m-%d}",
            "to": f"{datetime.fromtimestamp(t1, tz=timezone.utc):%Y-%m-%d}"}


def judge_trades(trades: list, anchor: date, p: dict) -> dict:
    """切段 + 判定(纯函数): 一份 trades → 各期 train/test 读数 + PASS/FAIL。
    返回 {periods, total, ok, warn}: periods 每期含生效门槛(min_pf);
    total = 整窗读数(列表页总净点/总笔数列)。"""
    a = anchor_dt(anchor)
    periods, all_ok, any_warn = [], True, False
    for s in p["segments"]:
        eff = s["min_pf"] if s["min_pf"] is not None else p["default_pf"]
        per = {"name": s["name"], "label": s["label"], "min_pf": eff}
        for part in ("train", "test"):
            st = _seg_stat(trades, *seg_window(a, s[part]), eff, p["min_seg_trades"])
            per[part] = st
            all_ok = all_ok and st["ok"]
            any_warn = any_warn or st["warn"]
        periods.append(per)
    t0 = (a - timedelta(days=window_years(p) * YEAR_DAYS)).timestamp()
    m = backtest._metrics([t for t in trades if t0 <= t["entry_time"] < a.timestamp()])
    return {"periods": periods, "ok": all_ok, "warn": any_warn,
            "total": {"n": m["trades"], "net": m.get("net_points", 0.0),
                      "pf": m.get("profit_factor"), "dd": m.get("max_dd_points")}}


def judge_one(strat: dict, bt_row, anchor: date, p: dict) -> dict:
    """单策略结论(纯函数): bt_row = backtests 行(dict) / None(缺行) / {"error": …}(任务失败)。
    铁则1: 缺行/失败一律 verdict=skip(永不归档)。返回报告明细一行。"""
    d = {"id": strat["id"], "name": strat["name"], "symbol": strat["symbol"],
         "status": strat["status"]}
    if isinstance(bt_row, dict) and "error" in bt_row and "trades" not in bt_row:
        d.update(verdict="skip", reason=f"回测失败: {bt_row['error']}")
        return d
    if bt_row is None:
        d.update(verdict="skip", reason="缺回测行")
        return d
    r = judge_trades(bt_row["trades"] or [], anchor, p)
    d.update(periods=r["periods"], total=r["total"], warn=r["warn"],
             verdict="pass" if r["ok"] else "fail")
    bad, warns = [], []
    for per in r["periods"]:
        for part, part_cn in (("train", "训练"), ("test", "测试")):
            st = per[part]
            if not st["ok"]:   # 不合格段 pf 必为数字(0笔/∞ 都算过, 进不了这里)
                bad.append(f"{per['label']}{part_cn} PF {st['pf']:g} ≤ {per['min_pf']:g}")
            if st["warn"]:
                warns.append(f"{per['label']}{part_cn} {st['n']}笔")
    d["reason"] = ("全段合格" if r["ok"] else "未过: " + " · ".join(bad)) \
        + (f"｜！样本不足: {' · '.join(warns)}" if warns else "")
    return d


def summarize(details: list, mode: str, archived: int, not_run: int) -> dict:
    return {"total": len(details),
            "passed": sum(1 for d in details if d["verdict"] == "pass"),
            "failed": sum(1 for d in details if d["verdict"] == "fail"),
            "skipped": sum(1 for d in details if d["verdict"] == "skip"),
            "warned": sum(1 for d in details if d.get("warn")),
            "archived": archived if mode == "execute" else 0,
            "not_run": not_run}


async def apply_actions(pool, mode: str, rid: int, tag_ids: list, archive_ids: list) -> None:
    """出池标签 + 执行动作(第6步):
      · 被判定过(pass/fail)一律追加 basis「oos_v2#<rid>」 — 预览也打(出池 + 报告溯源,
        只写 basis 一列不改 status); skip 不打, 下次重跑(铁则1)
      · 归档只在 execute: FAIL → ARCHIVED(死因 oos_v2_fail, 可逆),
        写入重申 status='CANDIDATE'(铁则2: 跑批期间被挂上机器的绝不动)"""
    if tag_ids:
        await pool.execute(
            "UPDATE strategies SET basis = CASE WHEN COALESCE(basis, '') = ''"
            " THEN $2 ELSE basis || '｜' || $2 END, updated_at = now()"
            " WHERE id = ANY($1)", tag_ids, f"{TAG}#{rid}")
    if mode == "execute" and archive_ids:
        await pool.execute(
            "UPDATE strategies SET status='ARCHIVED', archive_reason='oos_v2_fail',"
            " updated_at = now() WHERE id = ANY($1) AND status = 'CANDIDATE'", archive_ids)


async def settle(pool, cfg: dict, entries: list, errors: dict) -> tuple[int, dict]:
    """判定 + 落报告 + 出池标签/归档 — finalize(队列收尾) 与 run(全复用零任务直落) 共用。
    entries = [{"id","name","symbol"}](含现跑的和复用的); errors = {sid: 失败原因}(铁则1 → skip)。
    返回 (报告id, summary)。"""
    p = cfg["judge"]
    anchor = date.fromisoformat(cfg["anchor"])   # 锚点 = 提交那天(批内冻结, 不随收尾日漂移)
    mode = cfg["mode"]
    strat_ids = [int(e["id"]) for e in entries]
    # 策略现状临判现查(状态可能在跑批期间变了)
    strats = {r["id"]: dict(r) for r in await pool.fetch(
        "SELECT id, name, symbol, status FROM strategies WHERE id = ANY($1)", strat_ids)}
    # 回测行: 现跑的 = worker 刚 UPSERT 的新鲜数据; 复用的 = reuse_days 内的既有行(只取主品种)
    bt_rows = {r["strategy_id"]: dict(r) for r in await pool.fetch(
        "SELECT b.strategy_id, b.trades FROM backtests b"
        " JOIN strategies s ON s.id = b.strategy_id AND s.symbol = b.symbol"
        " WHERE b.strategy_id = ANY($1)", strat_ids)}
    details = list(cfg.get("skipped") or [])
    for e in entries:
        sid = int(e["id"])
        strat = strats.get(sid)
        if strat is None:   # 跑批期间被删
            details.append({"id": sid, "name": e.get("name"), "symbol": e.get("symbol"),
                            "status": "—", "verdict": "skip", "reason": "策略已删除"})
            continue
        bt = {"error": errors[sid]} if sid in errors else bt_rows.get(sid)
        details.append(judge_one(strat, bt, anchor, p))
    # 出池标签 = 被判定过的(pass/fail); 归档 = execute 下的 fail(skip 永不动)
    tag_ids = [d["id"] for d in details if d["verdict"] in ("pass", "fail")]
    archive_ids = [d["id"] for d in details if d["verdict"] == "fail"] \
        if mode == "execute" else []
    summary = summarize(details, mode, archived=len(archive_ids),
                        not_run=int(cfg.get("not_run") or 0))
    rid = await pool.fetchval(
        "INSERT INTO oos_v2_screens"
        " (mode, anchor, scope, params, summary, details, owner_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        mode, anchor, cfg["scope"], p, summary, details, int(cfg.get("owner") or 1))
    await apply_actions(pool, mode, rid, tag_ids, archive_ids)
    logger.info("oos_v2 report #%s (%s) 共%d 通过%d 未过%d 归档%d 跳过%d",
                rid, mode, summary["total"], summary["passed"],
                summary["failed"], summary["archived"], summary["skipped"])
    return rid, summary


async def finalize(pool: asyncpg.Pool) -> int | None:
    """全池清理的收尾(主节点心跳调用): 队列(kind=oos_v2)全跑完 → settle(判定/报告/标签/归档)
    → 清队列。返回报告 id; 没有待收尾的批次返回 None。
    并发安全: 整个收尾在 advisory lock 内; 结束即删空队列 → 别的副本看不到批次, 不会重复收尾。"""
    rows = await pool.fetch(
        "SELECT payload, status, error FROM jobs WHERE kind=$1", jobs.OOS_KIND)
    if not rows or any(r["status"] in ("PENDING", "RUNNING") for r in rows):
        return None      # 没批次 / 还在跑
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
            return None  # 另一个副本正在收尾
        try:
            # 锁内复查(拿锁期间对方可能已收尾并清空队列)
            n = await conn.fetchval("SELECT count(*) FROM jobs WHERE kind=$1", jobs.OOS_KIND)
            if not n:
                return None
            cfg = rows[0]["payload"]["run"]
            # 现跑条目来自队列(每策略一个任务, 只跑主品种), 失败记 error(铁则1 → skip);
            # 复用条目(reuse_days 内已有全窗回测行)冻结在 run_cfg 里, 一并判定
            entries, errors, seen = [], {}, set()
            for r in rows:
                pl = r["payload"]
                sid = int(pl["strategy_id"])
                if sid not in seen:
                    seen.add(sid)
                    entries.append({"id": sid, "name": pl.get("name"),
                                    "symbol": pl["symbol"]})
                if r["status"] == "FAILED":
                    errors[sid] = r["error"] or "未知原因"
            entries += [e for e in (cfg.get("reused") or []) if int(e["id"]) not in seen]
            rid, _ = await settle(pool, cfg, entries, errors)
            # 收尾完删空队列: "队列空 = 无待收尾批次"(幂等自清理, 不加表不加列)
            await pool.execute("DELETE FROM jobs WHERE kind=$1", jobs.OOS_KIND)
            return rid
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", LOCK_KEY)
