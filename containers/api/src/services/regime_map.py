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
            "min_tier_cell": int(c.get("min_tier_cell") or 10),
            # 样本不足提示线(2026-08-12 Frank 定, 与 oos_v2/矩阵页同款规矩):
            # 合计占比低于此值的类挂「！样本不足」—— 数字照给, 但不进统计量、不参与判定。
            # 为什么必须踢出统计量: Σ(观测-期望)²/期望 里期望越小单格贡献越炸 —
            # 跳空不利在 AAA 期望 11.1 实际 25, 光这一格就贡献 17.4, 能把 p 一手拉到 0.001,
            # 于是"盈亏在八格分层"和"跳空集中在某格"两件事混成一个 p, 谁都读不出来。
            # 踢掉之后 p 只回答一个干净问题: 这八种天气下赢的比例有没有真差别。
            # 0 = 关掉这道门(四类全参与判定); 用 is None 判空, 不能用 or —— 0 是有效值
            "min_tier_pct": float(10 if c.get("min_tier_pct") is None
                                  else c["min_tier_pct"])}


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


# 池化读数的分组(2026-08-13 与 Frank 定): 只此一条主判据, 事先定死, 不事后挑。
# 主判据 = 长短趋势【背离 vs 一致】—— 手算四份报告三份同向(+4.9/+2.5/+4.2), 待坐实。
# 机制: 双均线在【交叉那一刻】入场; 长短背离 = 方向刚转, 交叉信号新鲜;
#       长短一致 = 趋势确立已久, 此时的交叉多半是回调噪音。
# 三个单维是【对照组】: 它们都该接近 0 —— 若某个单维自己就有东西, 说明所谓"背离效应"
# 只是那一维的影子, 不是真交互。这是照妖镜, 不是凑数。
SPLIT_CN = {"trend": "长短背离", "long": "长趋势", "short": "短趋势", "vol": "波动"}


def splits(rows: list, tl: dict) -> dict:
    """按四种分法统计 (笔数, 止盈笔数) —— 池化的原料, 只计数不判定。

    分母含跳空两类(与表里显示的止盈率口径一致: 38.3% = 1412/3688), 不另开一套。
    返回 {分法: {边: [笔数, 止盈笔数]}}; 边 = A/B, 或 diverge/align。
    """
    out = {k: {"A": [0, 0], "B": [0, 0]} for k in ("long", "short", "vol")}
    out["trend"] = {"diverge": [0, 0], "align": [0, 0]}
    for r in rows:
        c = tl.get(datetime.fromtimestamp(r["ts"], tz=timezone.utc).date())
        if c not in CELLS:
            continue
        win = 1 if r["tier"] == "tp" else 0
        for i, k in enumerate(("long", "short", "vol")):
            b = out[k][c[i]]
            b[0] += 1
            b[1] += win
        b = out["trend"]["diverge" if c[0] != c[1] else "align"]
        b[0] += 1
        b[1] += win
    return out


def _stat(table: dict, tier_tot: dict, cell_tot: dict, n: int, tiers=TIERS) -> float:
    """列联表统计量: 卡方式的 Σ(观测-期望)²/期望 — 只作置换检验的统计量, 不查表。
    tiers = 参与判定的类(占比够的那几类); cell_tot/n 必须也是按这几类重算的, 否则期望值不对。"""
    s = 0.0
    for ti in tiers:
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


def permutation_p(rows: list, tl: dict, obs: float, n_perm: int, seed: int = 0,
                  tiers=TIERS) -> float:
    """置换检验: 保持每笔的【类别】不变, 只把格标签在交易之间随机重排 —
    切断"类别↔格"的对应, 保留四类占比与各格交易量结构。返回 p = (≥obs 的次数+1)/(N+1)"""
    # 只拿参与判定的类进检验(小类既不进统计量, 也不该进零分布)
    keep = set(tiers)
    pairs = [(r["tier"], tl.get(datetime.fromtimestamp(r["ts"], tz=timezone.utc).date()))
             for r in rows]
    pairs = [(t, c) for t, c in pairs if c in CELLS and t in keep]
    cells = [c for _t, c in pairs]
    tlab = [t for t, _c in pairs]
    if len(cells) < 20:
        return 1.0
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        rng.shuffle(cells)
        tb = {ti: {c: 0 for c in CELLS} for ti in keep}
        tt = {ti: 0 for ti in keep}
        ct = {c: 0 for c in CELLS}
        for ti, c in zip(tlab, cells):
            tb[ti][c] += 1
            tt[ti] += 1
            ct[c] += 1
        if _stat(tb, tt, ct, len(cells), tiers) >= obs:
            ge += 1
    return (ge + 1) / (n_perm + 1)


def analyze_version(rows: list, tl: dict, p: dict, seed: int = 0) -> dict:
    """单个版本的独立分析(不与其他版本发生任何关系)"""
    table, tier_tot, cell_tot, n, unl = contingency(rows, tl)
    sp = splits(rows, tl)          # 池化原料: 与逐策略判定无关, 任何情况都给
    if n < 20:
        return {"n": n, "unlabeled": unl, "splits": sp, "verdict": "skip",
                "reason": f"可贴格交易仅 {n} 笔(<20) — 时间线未覆盖或样本太少"}
    # 样本不足的类: 数字照给, 但不进统计量、不参与判定(与 oos_v2「！样本不足」同规矩)
    share = {ti: round(tier_tot[ti] / n * 100, 1) for ti in TIERS}
    judged = [ti for ti in TIERS if tier_tot[ti] and share[ti] >= p["min_tier_pct"]]
    small = [ti for ti in TIERS if tier_tot[ti] and share[ti] < p["min_tier_pct"]]
    if len(judged) < 2:
        return {"n": n, "unlabeled": unl, "splits": sp, "share": share, "judged": judged,
                "small": small, "table": table, "tier_tot": tier_tot,
                "cell_tot": cell_tot, "enrich": {}, "best": None, "verdict": "skip",
                "reason": f"参与判定的类不足 2 个(占比≥{p['min_tier_pct']:g}% 的只有"
                          f" {len(judged)} 类) — 无从比较"}
    # 判定用的缩减表: 只有参与判定的那几类, 格总数与总笔数都按它们重算(否则期望值不对)
    cell_j = {c: sum(table[ti][c] for ti in judged) for c in CELLS}
    n_j = sum(tier_tot[ti] for ti in judged)
    obs = _stat(table, tier_tot, cell_j, n_j, judged)
    pv = permutation_p(rows, tl, obs, p["permutations"], seed, judged)
    # 富集倍数对【全部】类都算(它只是个描述性倍数, 表里照常显示);
    # 但只有参与判定的类能触发「有信号」
    enrich, best = {}, None
    for ti in TIERS:
        if not tier_tot[ti]:
            continue
        for c in CELLS:
            if not cell_tot[c] or not table[ti][c]:
                continue
            e = (table[ti][c] / tier_tot[ti]) / (cell_tot[c] / n)
            enrich.setdefault(ti, {})[c] = round(e, 2)
            # 三道门都要过: 富集够 + 该格不是碎格 + 该类在该格笔数够; 且该类得参与判定
            if (ti in judged and e >= p["min_enrich"]
                    and cell_tot[c] >= p["min_cell_trades"]
                    and table[ti][c] >= p["min_tier_cell"]):
                if best is None or e > best["enrich"]:
                    best = {"tier": ti, "cell": c, "enrich": round(e, 2),
                            "cell_n": cell_tot[c], "n": table[ti][c]}
    jn = "、".join(TIER_CN[t] for t in judged)
    tail = ""
    if small:
        tail = ("　参与判定: " + jn + " · "
                + "、".join(f"{TIER_CN[t]}({share[t]}%)" for t in small)
                + f" 占比<{p['min_tier_pct']:g}% 挂！样本不足, 只提示不判定")
    if pv >= p["sig_p"]:
        verdict = "none"
        reason = (f"无信号: 置换 p={pv:.3f} ≥ {p['sig_p']:g} —"
                  f" 八个格之间的出场方式分布与随机贴标签无异" + tail)
    elif best:
        verdict = "signal"
        reason = (f"有信号: p={pv:.3f} · {TIER_CN[best['tier']]}在 {best['cell']} 富集"
                  f" {best['enrich']}x({best['n']}/{best['cell_n']}笔)" + tail)
    else:
        verdict = "weak"
        reason = (f"弱: p={pv:.3f} 显著但富集全靠碎格(无格同时满足 富集≥"
                  f"{p['min_enrich']}x、该格≥{p['min_cell_trades']}笔、"
                  f"该类在该格≥{p['min_tier_cell']}笔)" + tail)
    return {"n": n, "unlabeled": unl, "splits": sp, "share": share, "judged": judged,
            "small": small, "n_judged": n_j, "stat": round(obs, 2), "p": round(pv, 4),
            "table": table, "tier_tot": tier_tot, "cell_tot": cell_tot,
            "enrich": enrich, "best": best, "verdict": verdict, "reason": reason}
