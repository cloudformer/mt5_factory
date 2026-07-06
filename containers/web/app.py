"""MT5 Factory Web - Flask 前端

只做展示和转发: 所有数据/操作都走 app 的 HTTP API, 不含业务逻辑、不连数据库。
扩展方式: views/ 加一个 blueprint + templates/ 加一个页面。
"""
import os

from flask import Flask

from views.backtests import bp as backtests_bp
from views.dashboard import bp as dashboard_bp
from views.strategies import bp as strategies_bp

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "mt5web-dev")
app.register_blueprint(dashboard_bp)
app.register_blueprint(strategies_bp)
app.register_blueprint(backtests_bp)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
