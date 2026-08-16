"""策略实例统一收货管道(v2.2): 所有参数来源收敛到同一协议后走这一条路入库。

协议(与 AI 调参页第2/3步同一份合同):
    combos = [{"params": {...}, "basis": "依据/来源"}, ...]   (裸参数 dict 列表也接受)

来源与管道的关系:
    随机采样 / 网格展开 / AI调参(parent_id 谱系) / 未来DSL
        → 全部生产 combos → create_instances() 逐组校验→入库→逐组反馈
改校验规则、改插入行为 = 只改这里一处。
basis(批次)随实例入库, 与死因 archive_reason 对偶 — 家族溯源/成绩单负样本用。
"""
import logging
import re
from typing import Optional

from strategy_core import TEMPLATES

from src.services import usage

logger = logging.getLogger("instances")

_CELL_RE = re.compile(r"^[AB]{3}$")   # regime 门格键: 三字母八格


async def gate_error(pool, gate) -> Optional[str]:
    """metadata.regime 门校验(v0.3 六b, 克隆带门唯一写入口执法)。None=合格。
    规则: version 必须钉死真实版本id(null/default 拒收); cells 非空、键∈八格、
    倍率 0.5~1 且最多一位小数(与下拉档位一致); 未知键拒收。"""
    if not isinstance(gate, dict):
        return "regime 门必须是对象"
    extra = set(gate) - {"version", "cells"}
    if extra:
        return f"未知键 {sorted(extra)} — 只允许 version/cells"
    v = gate.get("version")
    if isinstance(v, bool) or not isinstance(v, int):
        return "version 必须是整数版本id — null/default 不收, 门必须钉死版本(v0.3)"
    if not await pool.fetchval("SELECT 1 FROM regime_versions WHERE id=$1", v):
        return f"版本 v{v} 不存在 — 去配置页看现有版本"
    cells = gate.get("cells")
    if not isinstance(cells, dict) or not cells:
        return "cells 不能为空 — 空门与父实例无差别, 拒绝创建"
    for k, mlt in cells.items():
        if not _CELL_RE.match(str(k)):
            return f"格键 {k!r} 非法 — 须为三字母八格(A/B ×3, 如 ABA)"
        if isinstance(mlt, bool) or not isinstance(mlt, (int, float)) \
                or not 0.5 <= float(mlt) <= 1:
            return f"格 {k} 倍率 {mlt!r} 出界 — 须 0.5~1(门只减仓不加仓)"
        if round(float(mlt), 1) != float(mlt):
            return f"格 {k} 倍率 {mlt!r} 精度过细 — 最多一位小数(下拉档位)"
    return None

DEFAULT_BATCH_LIMIT = 500  # 单批收货上限兜底; 实际值读 config 表 generate_batch_limit(生成页可改)


def trail_error(trail) -> Optional[str]:
    """trail 插件配置校验(v0.9, 插件调优收货用): 结构合法才收。None=合格"""
    if not isinstance(trail, dict):
        return "trail 必须是对象"
    t = trail.get("active")
    if t not in ("fixed", "breakeven", "atr"):
        return "trail.active 须为 fixed/breakeven/atr"
    p = trail.get(t)
    if not isinstance(p, dict):
        return f"trail 缺 {t} 参数组"
    if t == "atr":
        if not p.get("k") or p["k"] <= 0:
            return "trail.atr.k 须 >0"
    else:
        if not p.get("gap") or p["gap"] <= 0:
            return f"trail.{t}.gap 须 >0"
        if t == "breakeven" and not p.get("start"):
            return "trail.breakeven.start 必填(保本类必须有启动阈值)"
    return None


def combo_error(cls, space: dict, params) -> Optional[str]:
    """单组参数三层校验: 键完整 → 数值在空间范围内 → 模板 valid_params。None=合格。
    严格分离(2026-07-25 与 Frank 定): 生成策略管道**拒收 trail 等空间外键** —
    插件调优走第4步 trail_batch(内存批跑+保留写回), 两条线不混。"""
    keys = set(space)
    if not isinstance(params, dict) or set(params) != keys:
        return f"参数键必须恰好是 {sorted(keys)}(插件调优走第4步, 不在这里收 trail)"
    bad = next((k for k, v in params.items()
                if isinstance(space.get(k), tuple)
                and not space[k][0] <= v <= space[k][1]), None)
    if bad:
        return f"{bad}={params[bad]} 超出空间 {space[bad][:2]}"
    if not cls.valid_params(params):
        return "valid_params 不通过"
    return None


async def create_instances(pool, template: str, symbol: str, timeframe: str,
                           combos: list, parent_id: Optional[int] = None,
                           max_created: Optional[int] = None,
                           metadata: Optional[dict] = None, name_suffix: str = "",
                           trust_params: bool = False) -> dict:
    """逐组校验 → 入库(唯一约束去重, 可带 parent_id 谱系) → 逐组反馈 + 回读核验。

    每组结果 out:
      合格新建:   {"i", "params", "basis", "id", "verified"}   verified=库内params与请求逐字段一致
      已存在:     {"i", "params", "basis", "existing_id", "existing_status"}
      不合格:     {"i", "params", "basis", "error"}
    max_created: 新建满 N 个即停(随机模式"凑够 count 个新实例"用)。
    超上限不报错: 照收前 limit 组, 返回 truncated=被截断组数(调用方提示用户去配置页调大)。
    metadata/name_suffix/trust_params(v0.3 克隆带门): metadata=执行裁剪(空={}=全量),
    进唯一约束参与判重; name_suffix 附在名字后(如 -gate-v1-ABA1); trust_params=True
    跳过参数空间校验(克隆场景: 父参数来自库内现有行, 空间演化不应挡克隆)。"""
    cls = TEMPLATES[template]
    space = cls.RANDOM_SPACE or cls.PARAM_GRID
    # 单批收货上限(防失控倾倒; 随机模式按 count*5 采样也在此之下): config 可改, 兜底 500
    limit = await pool.fetchval(
        "SELECT value FROM config WHERE key='generate_batch_limit'") or DEFAULT_BATCH_LIMIT
    results, created_ids = [], []
    for i, item in enumerate(combos[:limit]):
        if max_created is not None and len(created_ids) >= max_created:
            break
        params = item.get("params", item) if isinstance(item, dict) else None
        basis = item.get("basis") if isinstance(item, dict) else None
        out = {"i": i + 1, "params": params, "basis": basis}
        err = None if trust_params else combo_error(cls, space, params)
        if err:
            out["error"] = err
            results.append(out)
            continue
        md = metadata or {}
        name = f"{template}-{symbol}-{timeframe}-" + \
               "-".join(f"{k}{params[k]}" for k in sorted(params)) + name_suffix
        row = await pool.fetchrow(
            "INSERT INTO strategies"
            " (name, template, symbol, timeframe, params, parent_id, basis, metadata)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING RETURNING id",
            name, template, symbol, timeframe, params, parent_id, basis, md)
        if row is None:  # 撞唯一约束 = 组合已存在(可能是死过的邻居) → 查现有ID给调用方
            # 判重按 (参数, metadata) 整体 — 同参数不同门是合法兄弟, 不能互相认领
            existing = await pool.fetchrow(
                "SELECT id, status FROM strategies"
                " WHERE template=$1 AND symbol=$2 AND timeframe=$3 AND params=$4"
                "   AND metadata = $5",
                template, symbol, timeframe, params, md)
            out["existing_id"] = existing["id"] if existing else None
            out["existing_status"] = existing["status"] if existing else None
            results.append(out)
            continue
        # 回读核验: 库里存的 params 必须与请求逐字段一致(防序列化/精度意外)
        stored = await pool.fetchval("SELECT params FROM strategies WHERE id=$1", row["id"])
        out["id"] = row["id"]
        out["verified"] = (stored == params)
        if not out["verified"]:
            out["stored_params"] = stored  # 极端情况暴露差异, 别静默
        created_ids.append(row["id"])
        results.append(out)
    truncated = max(0, len(combos) - limit)
    logger.info("create_instances %s@%s/%s: %d combos → created=%d truncated=%d parent=%s",
                template, symbol, timeframe, len(results), len(created_ids), truncated, parent_id)
    await usage.bump_by_owner(pool, "strategies_created", created_ids)  # 用量: 只记录不拦截
    return {"results": results, "created_ids": created_ids,
            "truncated": truncated, "batch_limit": limit}
