"""regime 筛选(v0.5)的判定与收尾 — 判定逻辑全系统只有这一份, 两条路共用:

  · 点名诊断: routes/regime_screen.py 内同步现跑回测 → judge_symbol/judge_one(只读不入库)
  · 全池清理: worker 并行跑完 jobs 队列(kind=regime_screen) → 本模块 finalize()
              由 api 心跳主节点调用(sync.py) — 报告/打标签/归档在这一步一次性发生

数据安全铁则(2026-08-05 与 Frank 定, 改这里前先读):
  1. 任一任务 FAILED / 缺回测行 / 窗口不足 → 该策略记"跳过(未判定)", **永不归档** — 缺数据绝不淘汰
  2. 归档写入时重新校验 status='CANDIDATE'(防运行期间被挂上机器)
  3. 预览模式除报告外零写入; 执行动作只在 finalize 里一次性发生(advisory lock 内, 不重复)
  4. 判定是纯读+纯计算; 引擎路径不在这里(worker 的 jobs._run_one 与批量回测同一份)
  5. 收尾完删空本工种队列 = "队列空即无待收尾批次", 幂等自清理(不加表不加列)
"""
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from src.services import jobs, regime

logger = logging.getLogger("screen")

TAG = "regime筛过"   # basis 标签词根: 已含即幂等跳过; 列表页搜"标签/生因"可一键捞幸存者
LOCK_KEY = 0x5C7EE41   # advisory lock: 同一时刻只有一个 api 副本在收尾


def cfg_params(cfg: dict) -> dict:
    """config regime_screen → 判据(带默认): run/plan/finalize 三处同一口径"""
    return {"window_years": float(cfg.get("window_years") or 5),
            "boundaries_years": sorted(cfg.get("boundaries_years") or [1, 2, 3, 4]),
            "min_cell_trades": int(cfg.get("min_cell_trades") or 5),
            "min_pass_cells": int(cfg.get("min_pass_cells") or 1),
            "min_pf": float(cfg.get("min_pf") if cfg.get("min_pf") is not None else 1.0)}


def _pf(gp: float, gl: float):
    """毛利/毛损 → PF; None=∞(无亏损有盈利), 0=没有盈利"""
    return round(gp / gl, 2) if gl > 0 else (None if gp > 0 else 0)


def _stat(n: int, gp: float, gl: float) -> dict:
    return {"n": n, "net": round(gp - gl, 1), "pf": _pf(gp, gl)}


async def judge_symbol(pool, tls: dict, bt: dict, vid: int, p: dict) -> dict:
    """单品种切片判定: 逐笔按入场日贴指定版本时间线(与九币矩阵同一口径, points×mult 加权),
    判定窗 = 近 window_years 年。每刀 = 近 b 年(后段) vs 剩余(前段)。
    段内合格 = 笔数≥地板 且 净点>阈值 且 PF>阈值(无亏损段 PF=∞ 恒过)。
    返回三层读数: 整窗 total / 每格 cells_stat / 每切分前后段 splits_stat + 合格格。"""
    sym = bt["symbol"]
    tl = tls.get(sym)
    if tl is None:      # 切谁治谁: 先自愈指定版本的时间线
        try:
            await regime.ensure_timeline(pool, sym, vid)
        except Exception as e:
            logger.warning("regime ensure %s v%s failed: %s", sym, vid, e)
        tl = tls[sym] = await regime.tl_map(pool, sym, vid)
    win_start = bt["to_time"] - timedelta(days=p["window_years"] * 365.25)
    tagged, unlabeled, cnt = [], 0, 0
    for t in (bt["trades"] or []):
        if t["entry_time"] < win_start.timestamp():
            continue
        cnt += 1
        cell = tl.get(datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).date())
        if cell is None:
            unlabeled += 1
            continue
        tagged.append((t["entry_time"], cell,
                       float(t.get("points") or 0) * float(t.get("mult") or 1)))
    floor, min_pf = p["min_cell_trades"], p["min_pf"]

    def _seg_ok(n, gp, gl):
        # 净点只显示不判定(2026-08-10 Frank 定): 单一按 PF 判 — 净点>0 与 PF>1 数学
        # 等价, 双条件会把 PF 阈值调到 1 以下时架空(容错带 0.9 生效不了)
        if n < floor:
            return False
        return (gp / gl > min_pf) if gl > 0 else gp > 0   # 无亏损段 PF=∞

    tot, per_cell = [0, 0.0, 0.0], {}
    for ts, cell, net in tagged:
        for acc in (tot, per_cell.setdefault(cell, [0, 0.0, 0.0])):
            acc[0] += 1
            if net >= 0:
                acc[1] += net
            else:
                acc[2] -= net
    qual, splits, splits_stat = None, {}, {}
    for y in p["boundaries_years"]:
        cut_ts = (bt["to_time"] - timedelta(days=y * 365.25)).timestamp()
        seg: dict = {}      # 格 → [剩余段 n/毛利/毛损, 近段 n/毛利/毛损]
        for ts, cell, net in tagged:
            s = seg.setdefault(cell, [0, 0.0, 0.0, 0, 0.0, 0.0])
            o = 0 if ts < cut_ts else 3
            s[o] += 1
            if net >= 0:
                s[o + 1] += net
            else:
                s[o + 2] -= net
        ok = {c for c, v in seg.items()
              if _seg_ok(v[0], v[1], v[2]) and _seg_ok(v[3], v[4], v[5])}
        splits[f"{y:g}"] = sorted(ok)
        splits_stat[f"{y:g}"] = {c: {"f": _stat(v[0], v[1], v[2]),
                                     "b": _stat(v[3], v[4], v[5])} for c, v in seg.items()}
        qual = ok if qual is None else (qual & ok)
    return {"symbol": sym, "trades": cnt, "unlabeled": unlabeled,
            "splits": splits, "cells": sorted(qual or ()),
            "total": _stat(*tot),
            "cells_stat": {c: _stat(*v) for c, v in sorted(per_cell.items())},
            "splits_stat": splits_stat,
            "window": f"{max(bt['from_time'], win_start):%Y-%m-%d} ~ {bt['to_time']:%Y-%m-%d}"}


def judge_one(strat: dict, res_map: dict, p: dict, symbols_mode: str) -> tuple:
    """单策略结论(纯函数): res_map = {品种: judge_symbol结果 / None(数据不足) / {"error":…}}
    返回 (明细 dict, 动作) — 动作 ∈ {"tag", "archive", None(跳过/只读)}。
    铁则1: 数据不足或失败一律 skip 不归档; 铁则2 的状态复核在写库处再做一次。"""
    d = {"id": strat["id"], "name": strat["name"], "symbol": strat["symbol"],
         "status": strat["status"]}
    main_res = res_map.get(strat["symbol"])
    if isinstance(main_res, dict) and "error" in main_res:
        d.update(verdict="skip", reason=f"回测失败: {main_res['error']}")
        return d, None
    if main_res is None:
        d.update(verdict="skip", reason=f"主品种 M1 覆盖不足总计 {p['window_years']:g} 年")
        return d, None
    results = [x for x in res_map.values()
               if isinstance(x, dict) and "error" not in x and x is not None]
    # 明细三层读数: 窗口/笔数按判定窗口径; 整窗 total → 每格 → 每切分前后段 → 结论
    d.update(window=main_res["window"], trades=main_res["trades"],
             splits=main_res["splits"], unlabeled=main_res["unlabeled"],
             pass_cells=main_res["cells"], total=main_res["total"],
             cells_stat=main_res["cells_stat"], splits_stat=main_res["splits_stat"])
    if symbols_mode == "all":
        d["cross"] = [{"symbol": x["symbol"], "pass_cells": x["cells"],
                       "trades": x["trades"]} for x in results
                      if x["symbol"] != strat["symbol"]]
    ok_list = [(x["symbol"], x["cells"]) for x in results
               if len(x["cells"]) >= p["min_pass_cells"]]
    readonly = strat["status"] != "CANDIDATE"   # 点名可带任意状态: 非空闲只读判定
    # 通过判定: 主货币=主品种达标; 全货币=任一品种存在合格格即过(发现型)
    ok = bool(ok_list) if symbols_mode == "all" \
        else len(main_res["cells"]) >= p["min_pass_cells"]
    if ok:
        d.update(verdict="pass",
                 reason="合格: " + " · ".join(f"{s} {'·'.join(c)}" for s, c in ok_list)
                 + ("(非空闲, 只记录不打标签)" if readonly else ""))
        return d, (None if readonly else "tag")
    d.update(verdict="fail",
             reason=("各品种均无" if symbols_mode == "all" else "无")
             + f"合格格(全切分达标格 < {p['min_pass_cells']} 个)"
             + ("(非空闲, 只记录不归档)" if readonly else ""))
    return d, (None if readonly else "archive")


def summarize(details: list, archive_ids: list, mode: str, not_run: int) -> dict:
    return {"total": len(details),
            "passed": sum(1 for d in details if d["verdict"] == "pass"),
            "failed": sum(1 for d in details if d["verdict"] == "fail"),
            "archived": len(archive_ids) if mode == "execute" else 0,
            "skipped": sum(1 for d in details if d["verdict"] == "skip"),
            "not_run": not_run}


async def apply_actions(pool, mode: str, rid: int, tag_ids: list, archive_ids: list) -> None:
    """执行动作(仅 mode=execute): 通过 → basis 追加标签; 未过 → 归档(死因可逆)。
    归档条件重申 status='CANDIDATE'(铁则2: 防运行期间被切状态)"""
    if mode != "execute":
        return
    if tag_ids:
        await pool.execute(
            "UPDATE strategies SET basis = CASE WHEN COALESCE(basis, '') = ''"
            " THEN $2 ELSE basis || '｜' || $2 END, updated_at = now()"
            " WHERE id = ANY($1)", tag_ids, f"{TAG}#{rid}")
    if archive_ids:
        await pool.execute(
            "UPDATE strategies SET status='ARCHIVED', archive_reason='regime_unstable',"
            " updated_at = now() WHERE id = ANY($1) AND status = 'CANDIDATE'", archive_ids)


async def finalize(pool: asyncpg.Pool) -> int | None:
    """全池收尾两态状态机(2026-08-08 与 Frank 定「所有回测统一」— 判定下放 worker,
    与 oos_v2 同款; 判定逻辑不变, 仍是本模块 judge_symbol/judge_one 唯一一份):

      状态A  回测任务(kind=regime_screen)全跑完 → 按策略归拢(含失败品种原因) →
             切 judge_chunk(全局 config, 默认300) 一块投「判定任务」(kind=regime_screen_judge)
             → 删回测任务。worker 并行判块(jobs._run_screen_judge), [明细,动作] 写回 result。
      状态B  判定任务全跑完 → 合并各块 → 单事务(报告+打标签+归档+删队列)提交 —
             任一步失败整体回滚, 下一拍重试从零, 不留孤儿报告。
             FAILED 的判定块 → 该块全员 skip(铁则1: 缺结果绝不淘汰)。

    背景: 老路径 api 单进程全内存判定 — 库里回测行刷成 20 年窗后, 全池收尾要拉 20+GB,
    在 6G 容器限额下必 OOM 死循环。并发安全: 整个收尾在 advisory lock 内。"""
    # ---- 状态B: 判定任务收口(优先查) ----
    jrows = await pool.fetch(
        "SELECT id, status, error, payload, result FROM jobs WHERE kind=$1 ORDER BY id",
        jobs.SCREEN_JUDGE_KIND)
    if jrows:
        if any(r["status"] in ("PENDING", "RUNNING") for r in jrows):
            return None
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
                return None
            try:
                n = await conn.fetchval(
                    "SELECT count(*) FROM jobs WHERE kind=$1", jobs.SCREEN_JUDGE_KIND)
                if not n:
                    return None
                cfg = jrows[0]["payload"]["run"]
                mode = cfg["mode"]
                details = list(cfg.get("skipped") or [])
                tag_ids, archive_ids = [], []
                for r in jrows:
                    if r["status"] == "DONE" and r["result"]:
                        for d, action in r["result"]:   # 块结果 = [明细, 动作] 对
                            details.append(d)
                            if action == "tag":
                                tag_ids.append(d["id"])
                            elif action == "archive":
                                archive_ids.append(d["id"])
                    else:   # 判定块失败(重试耗尽): 全员 skip, 永不淘汰(铁则1)
                        details += [{"id": int(e["id"]), "name": e.get("name"),
                                     "symbol": e.get("symbol"), "status": "—",
                                     "verdict": "skip",
                                     "reason": f"判定任务失败: {r['error'] or '未知原因'}"}
                                    for e in r["payload"]["chunk"]]
                archive_ids = archive_ids if mode == "execute" else []
                summary = summarize(details, archive_ids, mode,
                                    int(cfg.get("not_run") or 0))
                # 单事务收口: 报告+标签+归档+删队列同生共死(oos_v2 复读机事故的同款根治)
                async with conn.transaction():
                    rid = await conn.fetchval(
                        "INSERT INTO regime_screens"
                        " (mode, version_id, scope, params, summary, details, owner_id)"
                        " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
                        mode, int(cfg["version"]), cfg["scope"], cfg["judge"], summary,
                        details, int(cfg.get("owner") or 1))
                    await apply_actions(conn, mode, rid, tag_ids, archive_ids)
                    await conn.execute("DELETE FROM jobs WHERE kind = ANY($1)",
                                       [jobs.SCREEN_KIND, jobs.SCREEN_JUDGE_KIND])
                logger.info("regime_screen finalized: report #%s (%s) 共%d 通过%d 未过%d 归档%d",
                            rid, mode, summary["total"], summary["passed"],
                            summary["failed"], summary["archived"])
                return rid
            finally:
                await conn.fetchval("SELECT pg_advisory_unlock($1)", LOCK_KEY)

    # ---- 状态A: 回测任务收口 → 切块投判定任务 ----
    rows = await pool.fetch(
        "SELECT payload, status, error FROM jobs WHERE kind=$1", jobs.SCREEN_KIND)
    if not rows or any(r["status"] in ("PENDING", "RUNNING") for r in rows):
        return None      # 没批次 / 回测还在跑
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
            return None
        try:
            n = await conn.fetchval("SELECT count(*) FROM jobs WHERE kind=$1", jobs.SCREEN_KIND)
            if not n:
                return None
            cfg = rows[0]["payload"]["run"]
            # 按策略归拢: 失败的品种记 error(铁则1 → 该策略该品种保留失败态)
            entries, errors, seen = [], {}, set()
            for r in rows:
                pl = r["payload"]
                sid = int(pl["strategy_id"])
                if sid not in seen:
                    seen.add(sid)
                    entries.append({"id": sid, "name": pl.get("name"),
                                    "symbol": pl.get("main_symbol") or pl["symbol"]})
                if r["status"] == "FAILED":
                    errors.setdefault(str(sid), {})[pl["symbol"]] = r["error"] or "未知原因"
            chunk = int(await pool.fetchval(
                "SELECT value FROM config WHERE key='judge_chunk'") or 300)
            items = [{"chunk": entries[i:i + chunk], "errors": errors, "run": cfg,
                      "name": f"块{i // chunk + 1}",
                      "symbol": f"{len(entries[i:i + chunk])}个"}
                     for i in range(0, len(entries), chunk)]
            await jobs.submit_batch(pool, items, jobs.SCREEN_JUDGE_KIND)
            await pool.execute("DELETE FROM jobs WHERE kind=$1", jobs.SCREEN_KIND)
            logger.info("regime_screen judge submitted: %d chunks × ≤%d (共 %d 策略)",
                        len(items), chunk, len(entries))
            return None      # 报告在状态B出
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", LOCK_KEY)
