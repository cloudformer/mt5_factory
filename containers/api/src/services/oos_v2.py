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
  1. 任务 FAILED / 缺回测行 → 该策略记 skip, 永不归档也不盖履历 — 缺数据绝不淘汰, 下次重跑
  2. 归档写入时重申 status='CANDIDATE'(防跑批期间被挂上机器)
  3. 预览模式除【报告 + 出池履历(tags, schema/064)】外零写入(预览也追加履历,
     否则 6000 个永远跑不完一轮); 点名诊断恒零写入
  4. 判定是纯读+纯计算; 引擎路径不在这里(worker 的 jobs._run_one 与批量回测同一份)
  5. 收尾完删空本工种队列 = "队列空即无待收尾批次", 幂等自清理(不加表不加列)
"""
import logging
from datetime import date, datetime, timedelta, timezone

import asyncpg

from src.services import backtest, jobs

logger = logging.getLogger("oos_v2")

TAG = "oos_v2"           # 报告名词根: tags 元素 report=「oos_v2#<报告号>」= 出池 + 报告溯源
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
    # 复用天数不在这里: 全局唯一配置 backtest_reuse_days(2026-08-07 定, schema/062),
    # 判定在执行层 backtest.reuse_ok — 本模块无复用逻辑
    # 判定块大小不在这里: 全局机器参数 config.judge_chunk(schema/069, 2G worker 配 300),
    # v1/oos_v2 收尾共用 — 配置只在一处
    return {"segments": out,
            "default_pf": float(dp) if dp is not None else 1.0,
            "min_seg_trades": int(cfg.get("min_seg_trades") or 10),
            "batch_limit": int(cfg.get("batch_limit") or 50),
            # 单次上限硬顶(schema/067): UI 无编辑口, 改库直改(同一行 config 不分权限)
            "max_limit": int(cfg.get("max_limit") or 10000)}


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


async def apply_actions(pool, mode: str, rid: int,
                        pass_ids: list, fail_ids: list) -> None:
    """出池履历 + 执行动作(v0.7 批次1, 2026-08-08 Frank 定: tags 独立 list, 每批追加一个元素):
      · 被判定过的往 strategies.tags(JSONB 数组, schema/064)追加
        {"report": "oos_v2#<rid>", "status": "pass|fail", "created_time": <UTC时刻>}
        — 预览也追加(出池 + 报告溯源 + 列表一眼见结论); basis 不再写(回归出生证本职);
        结构化全量数据在报告里(report 可 JOIN), 元素只是带结论的指针。
        skip 不追加, 下次重跑(铁则1)
      · 归档只在 execute: FAIL → ARCHIVED(死因 oos_v2_fail, 可逆),
        写入重申 status='CANDIDATE'(铁则2: 跑批期间被挂上机器的绝不动)"""
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for ids, verdict in ((pass_ids, "pass"), (fail_ids, "fail")):
        if ids:
            await pool.execute(
                "UPDATE strategies SET tags = tags || $2::jsonb, updated_at = now()"
                " WHERE id = ANY($1)", ids,
                [{"report": f"{TAG}#{rid}", "status": verdict, "created_time": at}])
    if mode == "execute" and fail_ids:
        await pool.execute(
            "UPDATE strategies SET status='ARCHIVED', archive_reason='oos_v2_fail',"
            " updated_at = now() WHERE id = ANY($1) AND status = 'CANDIDATE'", fail_ids)


async def finish_report(db, cfg: dict, details: list) -> tuple[int, dict]:
    """合并出报告(收尾状态B的末段): summary → 落库 → 出池履历/归档。纯装配, 秒级。
    db = 事务中的连接(状态B把 报告+履历+归档+删队列 包成一个事务 — 任一步失败整体回滚,
    重试从头来, 绝不留半份报告复读; 2026-08-08 签名错配事故的教训)。"""
    mode = cfg["mode"]
    anchor = date.fromisoformat(cfg["anchor"])
    pass_ids = [d["id"] for d in details if d["verdict"] == "pass"]
    fail_ids = [d["id"] for d in details if d["verdict"] == "fail"]
    summary = summarize(details, mode,
                        archived=len(fail_ids) if mode == "execute" else 0,
                        not_run=int(cfg.get("not_run") or 0))
    rid = await db.fetchval(
        "INSERT INTO oos_v2_screens"
        " (mode, anchor, scope, params, summary, details, owner_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        mode, anchor, cfg["scope"], cfg["judge"], summary, details,
        int(cfg.get("owner") or 1))
    await apply_actions(db, mode, rid, pass_ids, fail_ids)
    logger.info("oos_v2 report #%s (%s) 共%d 通过%d 未过%d 归档%d 跳过%d",
                rid, mode, summary["total"], summary["passed"],
                summary["failed"], summary["archived"], summary["skipped"])
    return rid, summary


async def finalize(pool: asyncpg.Pool) -> int | None:
    """全池收尾两态状态机(2026-08-08 与 Frank 定: 判定下放 worker, api 只装配):

      状态A  回测任务(kind=oos_v2)全跑完 → 归拢策略清单/失败原因 → 切 judge_chunk(默认500)
             一块投「判定任务」(kind=oos_v2_judge, payload 带清单+运行配置) → 删回测任务。
             worker 并行判块(jobs._run_judge, 每块峰值≈1.5G 贴 worker 2G 预算),
             明细写回各 job 的 result 列。
      状态B  判定任务全跑完 → 合并各块 result → finish_report(报告/履历/归档) → 删光队列。
             FAILED 的判定块 → 该块全员 skip(铁则1: 缺结果绝不淘汰)。

    api 峰值从 27GB/单核20分钟 → 合并几十MB/秒级; 崩溃安全: 任一状态中断, 队列还在,
    下一拍心跳从当前状态续走(判定任务幂等可重跑)。并发安全: 整个收尾在 advisory lock 内。"""
    # ---- 状态B: 判定任务收口(优先查, 两态不会同时满足动作条件) ----
    jrows = await pool.fetch(
        "SELECT id, status, error, payload, result FROM jobs WHERE kind=$1 ORDER BY id",
        jobs.OOS_JUDGE_KIND)
    if jrows:
        if any(r["status"] in ("PENDING", "RUNNING") for r in jrows):
            return None      # 判定还在并行跑
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
                return None
            try:
                n = await conn.fetchval(
                    "SELECT count(*) FROM jobs WHERE kind=$1", jobs.OOS_JUDGE_KIND)
                if not n:
                    return None
                cfg = jrows[0]["payload"]["run"]
                details = list(cfg.get("skipped") or [])
                for r in jrows:
                    if r["status"] == "DONE" and r["result"]:
                        details += r["result"]
                    else:   # 判定块失败(重试耗尽): 全员 skip, 永不淘汰(铁则1)
                        details += [{"id": int(e["id"]), "name": e.get("name"),
                                     "symbol": e.get("symbol"), "status": "—",
                                     "verdict": "skip",
                                     "reason": f"判定任务失败: {r['error'] or '未知原因'}"}
                                    for e in r["payload"]["chunk"]]
                # 单事务收口(报告+履历+归档+删队列同生共死): 任一步失败整体回滚,
                # 下一拍重试从头来 — 不会留下没执行动作的孤儿报告(复读机事故的根治)
                async with conn.transaction():
                    rid, _ = await finish_report(conn, cfg, details)
                    await conn.execute("DELETE FROM jobs WHERE kind = ANY($1)",
                                       [jobs.OOS_KIND, jobs.OOS_JUDGE_KIND])
                return rid
            finally:
                await conn.fetchval("SELECT pg_advisory_unlock($1)", LOCK_KEY)

    # ---- 状态A: 回测任务收口 → 切块投判定任务 ----
    rows = await pool.fetch(
        "SELECT payload, status, error FROM jobs WHERE kind=$1", jobs.OOS_KIND)
    if not rows or any(r["status"] in ("PENDING", "RUNNING") for r in rows):
        return None      # 没批次 / 回测还在跑
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
            return None
        try:
            n = await conn.fetchval("SELECT count(*) FROM jobs WHERE kind=$1", jobs.OOS_KIND)
            if not n:
                return None
            cfg = rows[0]["payload"]["run"]
            entries, errors, seen = [], {}, set()
            for r in rows:
                pl = r["payload"]
                sid = int(pl["strategy_id"])
                if sid not in seen:
                    seen.add(sid)
                    entries.append({"id": sid, "name": pl.get("name"),
                                    "symbol": pl["symbol"]})
                if r["status"] == "FAILED":
                    errors[str(sid)] = r["error"] or "未知原因"
            entries += [e for e in (cfg.get("reused") or []) if int(e["id"]) not in seen]
            chunk = int(await pool.fetchval(
                "SELECT value FROM config WHERE key='judge_chunk'") or 300)
            items = [{"chunk": entries[i:i + chunk], "errors": errors, "run": cfg,
                      # name/symbol 只喂进度行显示(块3 @ 500个), 不参与执行
                      "name": f"块{i // chunk + 1}",
                      "symbol": f"{len(entries[i:i + chunk])}个"}
                     for i in range(0, len(entries), chunk)]
            await jobs.submit_batch(pool, items, jobs.OOS_JUDGE_KIND)
            await pool.execute("DELETE FROM jobs WHERE kind=$1", jobs.OOS_KIND)
            logger.info("oos_v2 judge submitted: %d chunks × ≤%d (共 %d 策略)",
                        len(items), chunk, len(entries))
            return None      # 报告在状态B出
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", LOCK_KEY)
