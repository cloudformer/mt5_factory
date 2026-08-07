"""v0.6 第5步 验收比对: oos_v2 报告明细 vs 用同一份回测行现场重算的判定 — 逐字段零差异才算数。

为什么这样比(照 v0.5 验收的规矩, "数据不能出错"):
  队列路径的收尾(services/oos_v2.finalize)与点名诊断路径调的是同一份判定函数
  (oos_v2.judge_one), 所以风险不在算法而在【管道】: 喂进去的回测行对不对、锚点冻结对不对、
  失败态处理有没有偏差。本脚本用报告里那批策略当前的 backtests 行 + 报告冻结的锚点/判据
  重新算一遍判定, 与报告存的明细逐字段比 —— 输入相同, 结果必须逐字节一致。

用法(在服务器上, 报告刚跑完就比, 期间别重跑这些策略的回测):
    docker compose exec api python /app/scripts/verify_oos_v2_report.py <报告id>
纯读, 不写任何东西。
"""
import asyncio
import json
import os
import sys
from datetime import date

import asyncpg

sys.path.insert(0, "/app")
from src.services import oos_v2   # noqa: E402  (sys.path 先就位)

FIELDS = ("verdict", "reason", "warn", "total", "periods")


async def main(rid: int) -> int:
    pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST", "postgres"), port=int(os.getenv("DB_PORT", "5432")),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"], min_size=1, max_size=4,
        init=lambda c: c.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                                        schema="pg_catalog"))
    rep = await pool.fetchrow(
        "SELECT anchor, params, details FROM oos_v2_screens WHERE id=$1", rid)
    if rep is None:
        print(f"报告 #{rid} 不存在")
        return 2
    p = rep["params"]
    anchor: date = rep["anchor"]
    judged_rows = [d for d in rep["details"] if d.get("verdict") != "skip"]
    print(f"报告 #{rid}: 明细 {len(rep['details'])} 行, 其中被判定 {len(judged_rows)} 行"
          f" (锚点 {anchor}, 判据={p})")

    diffs, checked = [], 0
    for d in judged_rows:
        sid = d["id"]
        strat = await pool.fetchrow(
            "SELECT id, name, symbol FROM strategies WHERE id=$1", sid)
        if strat is None:
            diffs.append((sid, "策略已删除, 无法重算(报告已存的结论保留)"))
            continue
        bt = await pool.fetchrow(
            "SELECT trades FROM backtests WHERE strategy_id=$1 AND symbol=$2",
            sid, strat["symbol"])
        # 报告里的状态是当时的; 重算沿用, 输入才逐字节等同
        recalc = oos_v2.judge_one({**dict(strat), "status": d.get("status")},
                                  dict(bt) if bt else None, anchor, p)
        checked += 1
        for f in FIELDS:
            a, b = d.get(f), recalc.get(f)
            if json.dumps(a, sort_keys=True, default=str) != \
                    json.dumps(b, sort_keys=True, default=str):
                diffs.append((sid, f"字段 {f} 不一致\n    报告: {a}\n    重算: {b}"))
    await pool.close()

    print(f"\n比对了 {checked} 个被判定策略 × {len(FIELDS)} 个字段"
          f"(periods 含每期 train/test 全部读数)")
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
