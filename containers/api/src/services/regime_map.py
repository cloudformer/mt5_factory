"""筛选·策略×regime 映射规律(2026-08-11 与 Frank 定稿) — 纯计算, 不跑引擎。

问题: 这个策略的盈亏, 能不能被某个 regime 口径的八个格分出层次?

方法(每个版本【独立】做, 铁律: 绝不跨版本比较 —— 不同版本的同名格是不同的分类维度,
"按性别分 vs 按上衣颜色分", 跨版本挑最好 = 数据挖掘, 分对了也是拟合):
  1. 交易按【出场原因】分四类(2026-08-11 Frank 定, 取代原 R 倍数分类):
     止盈/锁利 · 正常止损 · 跳空有利 · 跳空不利
     (R 倍数在固定 SL/TP 策略上必然退化: rr=2.4 → 所有止盈都 >2R 全是"大赢",
      佣金又把正常止损顶过 1R 全成"大亏" → 四类塌成两类, 分类白做;
      出场方式才带真信息, gap 两类 = 执行风险, 直接回答"哪种天气容易被跳空打穿")
  2. 先看不分格的四类占比 = 策略画像(少数大单型 / 高频小赚型…)
  3. 每版本一张 4类×8格 列联表 → 富集倍数 = 该类中此格占比 ÷ 此格整体交易占比
  4. 置换检验(保持每笔类别不变, 只打乱格标签, 默认 1000 次)出 p —
     不用卡方查表: 格大小极不均(452笔 vs 18笔)时卡方近似不准
  5. 结论三档: 有信号 = p<0.05 且某类在某格富集≥1.5、该格≥30笔、该类在该格≥10笔;
     弱 = p<0.05 但富集全靠碎格;
     无信号 = p≥0.05

数据: 复用 backtests.trades(不重跑引擎) + regime_timeline, 纯读库。
"""
import logging
import random
from datetime import datetime, timezone

logger = logging.getLogger("regime_map")

TAG = "regime_map"
CELLS = ("AAA", "AAB", "ABA", "ABB", "BAA", "BAB", "BBA", "BBB")
# 四类 = 出场原因(2026-08-11 Frank 选 A 方案): 固定 SL/TP 的策略下 R 倍数分类必然退化
# (rr=2.4 → 所有止盈都是 >2R 的"大赢"; 佣金还把正常止损顶成"大亏"), 而出场方式在不同
# 天气下的比例才是真有信息的 —— 尤其 gap 两类 = 执行风险(跳空/滑点), 直接回答
# "哪种天气容易被跳空打穿"。引擎 reason: tp/sl/tp_gap/sl_gap/tsl/tsl_gap
TIERS = ("tp", "sl", "gap_win", "gap_loss")
TIER_CN = {"tp": "止盈/锁利", "sl": "正常止损",
           "gap_win": "跳空有利", "gap_loss": "跳空不利"}


def cfg_params(cfg: dict) -> dict:
    """判据(带默认): 走页面表单每次现填, 只随报告存快照(不落 config)"""
    c = cfg or {}
    return {"tier_mode": "reason",
            "permutations": int(c.get("permutations") or 1000),
            "sig_p": float(c.get("sig_p") or 0.05),
            "min_enrich": float(c.get("min_enrich") or 1.5),
            "min_cell_trades": int(c.get("min_cell_trades") or 30),
            # 该类在该格的最小笔数(2026-08-11 修): 原来只管"该格总笔数", 3 笔算出的
            # 3.77x 被判成信号 — 假信号的根源
            "min_tier_cell": int(c.get("min_tier_cell") or 10)}


def tier_of(t: dict) -> str:
    """单笔 → 四类(按出场原因; 未知 reason 按盈亏兜底)。
    gap = 跳空/滑点越过价位成交(执行风险): 有利=意外之财, 不利=止损被打穿。"""
    r = (t.get("reason") or "").lower()
    pts = float(t.get("points") or 0) * float(t.get("mult") or 1)
    if "gap" in r:
        return "gap_win" if pts > 0 else "gap_loss"
    if r in ("tp", "tsl"):
        return "tp"
    if r == "sl":
        return "sl"
    return "tp" if pts > 0 else "sl"      # 手动/未知 reason: 按盈亏归入正常两类


def classify(trades: list, p: dict, point: float) -> tuple[list, dict]:
    """逐笔打四类标签 → (带标签的行, 四类计数)。行 = {ts, tier, pts}。
    p/point 保留在签名里(调用方通用), 出场原因分类本身不需要它们。"""
    rows, counts = [], {k: 0 for k in TIERS}
    for t in trades:
        tier = tier_of(t)
        counts[tier] += 1
        rows.append({"ts": int(t["entry_time"]), "tier": tier,
                     "pts": float(t.get("points") or 0) * float(t.get("mult") or 1)})
    return rows, counts


def _stat(table: dict, tier_tot: dict, cell_tot: dict, n: int) -> float:
    """列联表统计量: 卡方式的 Σ(观测-期望)²/期望 — 只作置换检验的统计量, 不查表"""
    s = 0.0
    for ti in TIERS:
        if not tier_tot[ti]:
            continue
        for c in CELLS:
            if not cell_tot[c]:
                continue
            exp = tier_tot[ti] * cell_tot[c] / n
            s += (table[ti][c] - exp) ** 2 / exp
    return s


def contingency(rows: list, tl: dict) -> tuple[dict, dict, dict, int, int]:
    """4类×8格 列联表。tl = {date: cell}; 无标签日的笔计入 unlabeled 不进表。
    返回 (table, tier_tot, cell_tot, n_labeled, n_unlabeled)"""
    table = {ti: {c: 0 for c in CELLS} for ti in TIERS}
    tier_tot = {ti: 0 for ti in TIERS}
    cell_tot = {c: 0 for c in CELLS}
    n, unl = 0, 0
    for r in rows:
        d = datetime.fromtimestamp(r["ts"], tz=timezone.utc).date()
        c = tl.get(d)
        if c not in CELLS:
            unl += 1
            continue
        table[r["tier"]][c] += 1
        tier_tot[r["tier"]] += 1
        cell_tot[c] += 1
        n += 1
    return table, tier_tot, cell_tot, n, unl


def permutation_p(rows: list, tl: dict, obs: float, n_perm: int, seed: int = 0) -> float:
    """置换检验: 保持每笔的【类别】不变, 只把格标签在交易之间随机重排 —
    切断"类别↔格"的对应, 保留四类占比与各格交易量结构。返回 p = (≥obs 的次数+1)/(N+1)"""
    cells = []
    for r in rows:
        d = datetime.fromtimestamp(r["ts"], tz=timezone.utc).date()
        c = tl.get(d)
        if c in CELLS:
            cells.append(c)
    tiers = [r["tier"] for r in rows
             if tl.get(datetime.fromtimestamp(r["ts"], tz=timezone.utc).date()) in CELLS]
    if len(cells) < 20:
        return 1.0
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        rng.shuffle(cells)
        tb = {ti: {c: 0 for c in CELLS} for ti in TIERS}
        tt = {ti: 0 for ti in TIERS}
        ct = {c: 0 for c in CELLS}
        for ti, c in zip(tiers, cells):
            tb[ti][c] += 1
            tt[ti] += 1
            ct[c] += 1
        if _stat(tb, tt, ct, len(cells)) >= obs:
            ge += 1
    return (ge + 1) / (n_perm + 1)


def analyze_version(rows: list, tl: dict, p: dict, seed: int = 0) -> dict:
    """单个版本的独立分析(不与其他版本发生任何关系)"""
    table, tier_tot, cell_tot, n, unl = contingency(rows, tl)
    if n < 20:
        return {"n": n, "unlabeled": unl, "verdict": "skip",
                "reason": f"可贴格交易仅 {n} 笔(<20) — 时间线未覆盖或样本太少"}
    obs = _stat(table, tier_tot, cell_tot, n)
    pv = permutation_p(rows, tl, obs, p["permutations"], seed)
    # 富集: 该类中此格占比 ÷ 此格整体交易占比; 只在"该格笔数够"时才算数
    enrich, best = {}, None
    for ti in TIERS:
        if not tier_tot[ti]:
            continue
        for c in CELLS:
            if not cell_tot[c] or not table[ti][c]:
                continue
            e = (table[ti][c] / tier_tot[ti]) / (cell_tot[c] / n)
            enrich.setdefault(ti, {})[c] = round(e, 2)
            # 两道笔数门槛都要过: 该格总笔数够(格本身不是碎格) + 该类在该格笔数够
            # (2026-08-11 修: 原来只管前者, "小赢在ABA富集3.77x" 底下只有 3 笔 → 假信号)
            if (e >= p["min_enrich"] and cell_tot[c] >= p["min_cell_trades"]
                    and table[ti][c] >= p["min_tier_cell"]):
                if best is None or e > best["enrich"]:
                    best = {"tier": ti, "cell": c, "enrich": round(e, 2),
                            "cell_n": cell_tot[c], "n": table[ti][c]}
    if pv >= p["sig_p"]:
        verdict, reason = "none", f"无信号: 置换 p={pv:.3f} ≥ {p['sig_p']:g} — 四类在八格的分布与随机贴标签无异"
    elif best:
        verdict = "signal"
        reason = (f"有信号: p={pv:.3f} · {TIER_CN[best['tier']]}在 {best['cell']} 富集"
                  f" {best['enrich']}x({best['n']}/{best['cell_n']}笔)")
    else:
        verdict = "weak"
        reason = (f"弱: p={pv:.3f} 显著但富集全靠小格子"
                  f"(无格同时满足 富集≥{p['min_enrich']}x、该格≥{p['min_cell_trades']}笔、该类在该格≥{p['min_tier_cell']}笔)")
    return {"n": n, "unlabeled": unl, "stat": round(obs, 2), "p": round(pv, 4),
            "table": table, "tier_tot": tier_tot, "cell_tot": cell_tot,
            "enrich": enrich, "best": best, "verdict": verdict, "reason": reason}
