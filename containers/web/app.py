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
from views.strategies import bp as strategies_bp
from views.symbols import bp as symbols_bp
from views.users import bp as admin_bp
from views.workers import bp as workers_bp

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "mt5web-dev")

# 开发模式身份(v5.6 登录前的过渡, 2026-07-25 与 Frank 定): 右上角显示当前用户并可切换,
# 存 session 仅做标识 — 不做任何过滤/权限(那是 5.6 通电的事); 全部测试好了才加登录页。
_users_cache = {"t": 0.0, "users": []}


@app.context_processor
def inject_dev_identity():
    import time as _time

    from flask import session

    import api_client as _api
    if _time.time() - _users_cache["t"] > 60:   # 60s 轻缓存: 每页一次 api 调用太浪费
        try:
            _users_cache["users"] = _api.get("/users")["users"]
            _users_cache["t"] = _time.time()
        except Exception:
            pass
    users = _users_cache["users"]
    uid = session.get("dev_user_id")
    if uid is None:   # 默认身份 = frank(交易用户, 日常视角); 想看管理视角切 admin
        uid = next((u["id"] for u in users if u["name"] == "frank"), 1)
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
app.register_blueprint(admin_bp)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
