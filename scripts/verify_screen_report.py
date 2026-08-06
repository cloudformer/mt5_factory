"""步骤5 验收比对: 报告明细 vs 用同一份回测行现场重算的判定 — 逐字段零差异才算数。

为什么这样比(2026-08-05 与 Frank 定"数据不能出错"):
  队列路径的收尾(services/screen.finalize)与点名诊断路径调的是同一份判定函数
  (screen.judge_symbol / judge_one), 所以风险不在算法而在【管道】: 喂进去的回测行对不对、
  窗口检查/货币模式/失败态处理有没有偏差。本脚本就用报告里那批策略当前的 backtests 行
  重新算一遍判定, 与报告存的明细逐字段比 —— 输入相同, 结果必须逐字节一致。

用法(在服务器上, 报告刚跑完就比, 期间别重跑这些策略的回测):
    docker compose exec api python /app/scripts/verify_screen_report.py <报告id>
纯读, 不写任何东西。
"""
import asyncio
import json
import os
import sys

import asyncpg

sys.path.insert(0, "/app")
from src.services import screen   # noqa: E402  (sys.path 先就位)

FIELDS = ("verdict", "reason", "window", "trades", "unlabeled", "pass_cells",
          "total", "splits", "cells_stat", "splits_stat", "cross")


async def main(rid: int) -> int:
    pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST", "postgres"), port=int(os.getenv("DB_PORT", "5432")),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"], min_size=1, max_size=4,
        init=lambda c: c.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                                        schema="pg_catalog"))
    rep = await pool.fetchrow(
        "SELECT version_id, scope, params, details FROM regime_screens WHERE id=$1", rid)
    if rep is None:
        print(f"报告 #{rid} 不存在")
        return 2
    p, vid = rep["params"], rep["version_id"]
    symbols_mode = (rep["scope"] or {}).get("symbols", "main")
    need_days = int(p["window_years"] * 365.25) - 45
    judged_rows = [d for d in rep["details"] if d.get("verdict") != "skip"]
    print(f"报告 #{rid}: 明细 {len(rep['details'])} 行, 其中被判定 {len(judged_rows)} 行"
          f" (regime v{vid}, 货币={symbols_mode}, 判据={p})")

    tls: dict = {}
    diffs, checked = [], 0
    for d in judged_rows:
        sid = d["id"]
        strat = await pool.fetchrow(
            "SELECT id, name, symbol, status FROM strategies WHERE id=$1", sid)
        if strat is None:
            diffs.append((sid, "策略已删除, 无法重算(报告已存的结论保留)"))
            continue
        res_map: dict = {}
        for bt in await pool.fetch(
                "SELECT symbol, from_time, to_time, trades FROM backtests"
                " WHERE strategy_id=$1", sid):
            sym = bt["symbol"]
            if symbols_mode == "main" and sym != strat["symbol"]:
                continue
            if (bt["to_time"] - bt["from_time"]).days < need_days:
                res_map[sym] = None
                continue
            res_map[sym] = await screen.judge_symbol(pool, tls, dict(bt), vid, p)
        if strat["symbol"] not in res_map:
            res_map[strat["symbol"]] = None
        # 报告里的状态是当时的; 重算用当时状态才公平(状态影响 readonly → reason 文案)
        recalc, _ = screen.judge_one({**dict(strat), "status": d.get("status")},
                                     res_map, p, symbols_mode)
        checked += 1
        for f in FIELDS:
            a, b = d.get(f), recalc.get(f)
            if json.dumps(a, sort_keys=True, default=str) != \
                    json.dumps(b, sort_keys=True, default=str):
                diffs.append((sid, f"字段 {f} 不一致\n    报告: {a}\n    重算: {b}"))
    await pool.close()

    print(f"\n比对了 {checked} 个被判定策略 × {len(FIELDS)} 个字段")
    if diffs:
        print(f"发现 {len(diffs)} 处差异 —— 不通过:")
        for sid, msg in diffs[:20]:
            print(f"  #{sid}: {msg}")
        if len(diffs) > 20:
            print(f"  …另有 {len(diffs) - 20} 处")
        return 1
    print("零差异 —— 队列路径的判定与同一份数据现场重算完全一致 ✓")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(int(sys.argv[1]))))
