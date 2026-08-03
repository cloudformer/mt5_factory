"""MT5 Factory Web - Flask 前端

只做展示和转发: 所有数据/操作都走 api 的 HTTP 接口, 不含业务逻辑、不连数据库。
扩展方式: views/ 加一个 blueprint + templates/ 加一个页面。
"""
import os

from flask import Flask

from views.backtests import bp as backtests_bp
from views.dashboard import bp as dashboard_bp
from views.datasync import bp as datasync_bp
from views.execution import bp as execution_bp
from views.mt5 import bp as mt5_bp
from views.regime_screen import bp as regime_screen_bp
from views.strategies import bp as strategies_bp
from views.symbols import bp as symbols_bp
from views.users import bp as admin_bp
from views.workers import bp as workers_bp

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "mt5web-dev")

# 开发模式身份(v5.6 登录前的过渡): 右上角显示当前用户并可切换, 存 session。
# 2026-07-26 通电(读路径过滤): 该身份随 api_client 以 X-User-Id 头捎给 api,
# api 在列表类读路径按它过滤(资产只见自己的); 仍零拦截无登录, 测试好了才加登录页。
_users_cache = {"t": 0.0, "users": []}


def _dev_users() -> list:
    import time as _time

    import api_client as _api
    if _time.time() - _users_cache["t"] > 60:   # 60s 轻缓存: 每页一次 api 调用太浪费
        try:
            _users_cache["users"] = _api.get("/users")["users"]
            _users_cache["t"] = _time.time()
        except Exception:
            pass
    return _users_cache["users"]


@app.before_request
def ensure_dev_identity():
    """进视图前把身份落进 session — api_client 靠它发 X-User-Id, 首次访问也不缺身份。
    默认身份 = frank(交易用户, 日常视角); 想看管理视角右上角切 admin。"""
    from flask import request, session
    if request.endpoint in ("healthz", "static"):   # 健康检查/静态文件不需要身份
        return
    if session.get("dev_user_id") is None:
        users = _dev_users()
        session["dev_user_id"] = next(
            (u["id"] for u in users if u["name"] == "frank"), 1)


@app.context_processor
def inject_dev_identity():
    from flask import session
    users = _dev_users()
    uid = session.get("dev_user_id")
    name = next((u["name"] for u in users if u["id"] == uid), "?")
    return {"dev_users": users, "dev_user_id": uid, "dev_user_name": name}
app.register_blueprint(dashboard_bp)
app.register_blueprint(workers_bp)
app.register_blueprint(symbols_bp)
app.register_blueprint(datasync_bp)
app.register_blueprint(strategies_bp)
app.register_blueprint(backtests_bp)
app.register_blueprint(execution_bp)
app.register_blueprint(mt5_bp)
app.register_blueprint(regime_screen_bp)
app.register_blueprint(admin_bp)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
