"""路由注册表 — 加新一组 API: 新建文件 + 在这里 import 并加入 ROUTERS"""
from src.routes.backtests import router as backtests_router
from src.routes.data import router as data_router
from src.routes.hosts import router as hosts_router
from src.routes.strategies import router as strategies_router
from src.routes.symbols import router as symbols_router

ROUTERS = [hosts_router, data_router, strategies_router, backtests_router, symbols_router]
