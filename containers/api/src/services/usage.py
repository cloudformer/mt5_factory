"""用量计数(schema/041, 2026-07-25 定: 只记录不拦截) — "跑完加一条"哲学:
入口处一次 +N(不是每笔一次), 单行 UPSERT 亚毫秒, 相对业务本身是尘埃级负荷。
计数失败只告警不拖垮业务(它是观察不是账本)。"""
import logging

logger = logging.getLogger("usage")

# 跨天/跨月翻篇: 同期累加, 新期覆盖(归零=被新值覆盖, 无定时任务; 月与日同款, schema/071)
_UPSERT_TAIL = (
    " ON CONFLICT (user_id, metric) DO UPDATE SET"
    "   used_total = usage_counters.used_total + EXCLUDED.used_total,"
    "   day_used = CASE WHEN usage_counters.day = CURRENT_DATE"
    "                   THEN usage_counters.day_used + EXCLUDED.day_used"
    "                   ELSE EXCLUDED.day_used END,"
    "   day = CURRENT_DATE,"
    "   month_used = CASE WHEN usage_counters.month = date_trunc('month', CURRENT_DATE)"
    "                     THEN usage_counters.month_used + EXCLUDED.month_used"
    "                     ELSE EXCLUDED.month_used END,"
    "   month = date_trunc('month', CURRENT_DATE), updated_at = now()"
)


async def bump_by_owner(pool, metric: str, strategy_ids: list) -> None:
    """按策略 owner 归账(web 无登录也能记: 记给资源主人)。
    strategy_ids 可含重复(回测的 策略×品种 每个 job 算一次), unnest 保留重复计数。"""
    if not strategy_ids:
        return
    try:
        await pool.execute(
            "INSERT INTO usage_counters"
            " (user_id, metric, used_total, day, day_used, month, month_used)"
            " SELECT s.owner_id, $2, count(*), CURRENT_DATE, count(*),"
            "        date_trunc('month', CURRENT_DATE), count(*)"
            "   FROM unnest($1::int[]) AS t(sid) JOIN strategies s ON s.id = t.sid"
            "  GROUP BY s.owner_id" + _UPSERT_TAIL,
            strategy_ids, metric)
    except Exception as e:
        logger.warning("usage bump failed (%s, %d ids): %s", metric, len(strategy_ids), e)
