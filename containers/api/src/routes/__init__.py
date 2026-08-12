"""路由注册表 — 加新一组 API: 新建文件 + 在这里 import 并加入 ROUTERS"""
from src.routes.backtests import router as backtests_router
from src.routes.data import router as data_router
from src.routes.hosts import router as hosts_router
from src.routes.oos_v2 import router as oos_v2_router
from src.routes.regime_map import router as regime_map_router
from src.routes.regime_screen import router as regime_screen_router
from src.routes.strategies import router as strategies_router
from src.routes.symbols import router as symbols_router
from src.routes.users import router as users_router

ROUTERS = [hosts_router, data_router, strategies_router, backtests_router, symbols_router,
           users_router, regime_screen_router, oos_v2_router, regime_map_router]
