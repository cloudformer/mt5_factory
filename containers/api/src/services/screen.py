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
            "min_net_points": float(cfg.get("min_net_points") or 0),
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
    floor, min_net, min_pf = p["min_cell_trades"], p["min_net_points"], p["min_pf"]

    def _seg_ok(n, gp, gl):
        if n < floor or gp - gl <= min_net:
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
    """全池清理的收尾(主节点心跳调用): 队列全跑完 → 切片判定 → 落报告 → 执行动作 → 清队列。
    返回报告 id; 没有待收尾的批次返回 None。
    并发安全: 整个收尾在 advisory lock 内; 结束即删空队列 → 别的副本看不到批次, 不会重复收尾。"""
    rows = await pool.fetch(
        "SELECT payload, status, error FROM jobs WHERE kind=$1", jobs.SCREEN_KIND)
    if not rows or any(r["status"] in ("PENDING", "RUNNING") for r in rows):
        return None      # 没批次 / 还在跑
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
            return None  # 另一个副本正在收尾
        try:
            # 锁内复查(拿锁期间对方可能已收尾并清空队列)
            n = await conn.fetchval("SELECT count(*) FROM jobs WHERE kind=$1", jobs.SCREEN_KIND)
            if not n:
                return None
            cfg = rows[0]["payload"]["run"]
            p, vid = cfg["judge"], int(cfg["version"])
            mode, symbols_mode = cfg["mode"], cfg["symbols"]
            need_days = int(p["window_years"] * 365.25) - 45
            # 任务按策略归拢: 失败的品种记 error(铁则1 → 该策略跳过不归档)
            by_sid: dict = {}
            strat_ids = []
            for r in rows:
                pl = r["payload"]
                sid = int(pl["strategy_id"])
                if sid not in by_sid:
                    by_sid[sid] = {"main": pl.get("main_symbol") or pl["symbol"],
                                   "name": pl.get("name"), "errors": {}}
                    strat_ids.append(sid)
                if r["status"] == "FAILED":
                    by_sid[sid]["errors"][pl["symbol"]] = r["error"] or "未知原因"
            # 策略现状临判现查(状态可能在跑批期间变了 — 铁则2)
            strats = {r["id"]: dict(r) for r in await pool.fetch(
                "SELECT id, name, symbol, status FROM strategies WHERE id = ANY($1)",
                strat_ids)}
            # 回测行 = worker 刚跑完 UPSERT 的新鲜数据
            bt_rows: dict = {}
            for r in await pool.fetch(
                    "SELECT strategy_id, symbol, from_time, to_time, trades FROM backtests"
                    " WHERE strategy_id = ANY($1)", strat_ids):
                bt_rows.setdefault(r["strategy_id"], {})[r["symbol"]] = r
            tls: dict = {}
            details, tag_ids, archive_ids = list(cfg.get("skipped") or []), [], []
            for sid in strat_ids:
                info = by_sid[sid]
                strat = strats.get(sid)
                if strat is None:   # 跑批期间被删
                    details.append({"id": sid, "name": info["name"], "symbol": info["main"],
                                    "status": "—", "verdict": "skip", "reason": "策略已删除"})
                    continue
                res_map: dict = {}
                for sym, err in info["errors"].items():
                    res_map[sym] = {"error": err}
                for sym, bt in (bt_rows.get(sid) or {}).items():
                    if sym in res_map:
                        continue          # 该品种任务失败: 保留失败态, 不用旧行冒充
                    if symbols_mode == "main" and sym != strat["symbol"]:
                        continue          # 主货币模式只判主品种
                    if (bt["to_time"] - bt["from_time"]).days < need_days:
                        res_map[sym] = None   # 窗口不足: 主品种→跳过; 跨品种→不纳入要求
                        continue
                    res_map[sym] = await judge_symbol(pool, tls, dict(bt), vid, p)
                if strat["symbol"] not in res_map:
                    res_map[strat["symbol"]] = None   # 主品种连回测行都没有
                d, action = judge_one(strat, res_map, p, symbols_mode)
                details.append(d)
                if action == "tag":
                    tag_ids.append(sid)
                elif action == "archive":
                    archive_ids.append(sid)
            summary = summarize(details, archive_ids, mode, int(cfg.get("not_run") or 0))
            rid = await pool.fetchval(
                "INSERT INTO regime_screens"
                " (mode, version_id, scope, params, summary, details, owner_id)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
                mode, vid, cfg["scope"], p, summary, details, int(cfg.get("owner") or 1))
            await apply_actions(pool, mode, rid, tag_ids, archive_ids)
            # 收尾完删空队列: "队列空 = 无待收尾批次"(幂等自清理, 不加表不加列)
            await pool.execute("DELETE FROM jobs WHERE kind=$1", jobs.SCREEN_KIND)
            logger.info("regime_screen finalized: report #%s (%s) 共%d 通过%d 未过%d 归档%d",
                        rid, mode, summary["total"], summary["passed"], summary["failed"],
                        summary["archived"])
            return rid
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", LOCK_KEY)
