"""身份口径(v5.6 通电, 2026-07-26 与 Frank 定): 读路径按当前用户过滤资产。

规则只有一条: scope_uid = 过滤用的 owner_id —
  无身份(内部调用/脚本/未换钥匙的 runner) → None = 不过滤(全量, 与通电前行为一致)
  owner(id 1)                              → None = 不过滤(全局视野: 总视图/总报表/调试)
  其他用户                                  → 本人 id = 只见自己的资产
身份来源见 main.identify_caller(X-API-Key 或登录前过渡的 X-User-Id); 仍零拦截无 401。
"""
from fastapi import HTTPException

OWNER_ID = 1   # 角色模型: id 是身份, name 只是标签 — id 1 即 owner


def scope_uid(request):
    """列表类读端点取过滤 uid: None = 不加 owner 条件"""
    uid = getattr(request.state, "user_id", None)
    return None if uid == OWNER_ID else uid


async def _assert_owned(pool, request, table: str, row_id: int, what: str) -> None:
    """按 id 的入口守门: 资产存在且不属于当前用户 → 404(不暴露存在性)。
    无身份/owner = 放行; 资产不存在 = 放行(交给端点自己的 404, 语义一致)。"""
    uid = scope_uid(request)
    if uid is None:
        return
    owner = await pool.fetchval(f"SELECT owner_id FROM {table} WHERE id=$1", row_id)
    if owner is not None and owner != uid:
        raise HTTPException(status_code=404, detail=f"{what} not found")


async def assert_strategy_visible(pool, request, strategy_id: int) -> None:
    await _assert_owned(pool, request, "strategies", strategy_id, "strategy")


async def assert_host_visible(pool, request, host_id: int) -> None:
    await _assert_owned(pool, request, "mt5_hosts", host_id, "host")
