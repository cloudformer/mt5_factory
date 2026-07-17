"""策略实例统一收货管道(v2.2): 所有参数来源收敛到同一协议后走这一条路入库。

协议(与 AI 调参页第2/3步同一份合同):
    combos = [{"params": {...}, "basis": "依据/来源"}, ...]   (裸参数 dict 列表也接受)

来源与管道的关系:
    随机采样 / 网格展开 / AI调参(parent_id 谱系) / 旧AI协议 / 未来DSL
        → 全部生产 combos → create_instances() 逐组校验→入库→逐组反馈
改校验规则、改插入行为 = 只改这里一处。
"""
import logging
from typing import Optional

from strategy_core import TEMPLATES

logger = logging.getLogger("instances")

MAX_BATCH = 500  # 单次收货上限(防失控倾倒; 随机模式按 count*5 采样也在此之下)


def combo_error(cls, space: dict, params) -> Optional[str]:
    """单组参数三层校验: 键完整 → 数值在空间范围内 → 模板 valid_params。None=合格"""
    keys = set(space)
    if not isinstance(params, dict) or set(params) != keys:
        return f"参数键必须恰好是 {sorted(keys)}"
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
                           max_created: Optional[int] = None) -> dict:
    """逐组校验 → 入库(唯一约束去重, 可带 parent_id 谱系) → 逐组反馈 + 回读核验。

    每组结果 out:
      合格新建:   {"i", "params", "basis", "id", "verified"}   verified=库内params与请求逐字段一致
      已存在:     {"i", "params", "basis", "existing_id", "existing_status"}
      不合格:     {"i", "params", "basis", "error"}
    max_created: 新建满 N 个即停(随机模式"凑够 count 个新实例"用)。"""
    cls = TEMPLATES[template]
    space = cls.RANDOM_SPACE or cls.PARAM_GRID
    results, created_ids = [], []
    for i, item in enumerate(combos[:MAX_BATCH]):
        if max_created is not None and len(created_ids) >= max_created:
            break
        params = item.get("params", item) if isinstance(item, dict) else None
        basis = item.get("basis") if isinstance(item, dict) else None
        out = {"i": i + 1, "params": params, "basis": basis}
        err = combo_error(cls, space, params)
        if err:
            out["error"] = err
            results.append(out)
            continue
        name = f"{template}-{symbol}-{timeframe}-" + \
               "-".join(f"{k}{params[k]}" for k in sorted(params))
        row = await pool.fetchrow(
            "INSERT INTO strategies (name, template, symbol, timeframe, params, parent_id)"
            " VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING RETURNING id",
            name, template, symbol, timeframe, params, parent_id)
        if row is None:  # 撞唯一约束 = 组合已存在(可能是死过的邻居) → 查现有ID给调用方
            existing = await pool.fetchrow(
                "SELECT id, status FROM strategies"
                " WHERE template=$1 AND symbol=$2 AND timeframe=$3 AND params=$4",
                template, symbol, timeframe, params)
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
    logger.info("create_instances %s@%s/%s: %d combos → created=%d parent=%s",
                template, symbol, timeframe, len(results), len(created_ids), parent_id)
    return {"results": results, "created_ids": created_ids}
