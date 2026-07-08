"""MT5 Bridge (attach-only rewrite) - Windows worker HTTP API, port = env MT5_PORT (8020).

哑执行器: 只做 MT5 <-> HTTP 转换。连接部分全部委托给 mt5_conn (冻结的 attach-only 核心),
本文件不含任何拉终端/自愈/MT5_PATH 逻辑 —— 前提是人已把 MT5 终端开好并登录 (demo 账户会记住)。

MT5 账户来源: 1) api 远程下发 POST /connect  2) env MT5_LOGIN/PASSWORD/SERVER
             3) 都没有 -> 附着到本机已登录的终端 (推荐, 最省事)
"""
import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# import the frozen connection core (windows_mt5/conn/, shared with runner)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conn import mt5_conn        # noqa: E402
from conn.paths import ENV_FILE  # noqa: E402  (repo 根/env 定位, 无魔法层级数字)

# 统一配置: 与 Linux docker compose 共用 env/.dev.env
load_dotenv(ENV_FILE)

BRIDGE_PORT = int(os.getenv("MT5_PORT", "8020"))
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")
DOCKER_COMPOSE_HOST = os.getenv("DOCKER_COMPOSE_HOST", "").strip()
API_PORT = os.getenv("API_PORT", "8010")
RUNNER_STATUS_FILE = Path(__file__).resolve().parents[1] / "runner_status.json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
)
logger = logging.getLogger("bridge")

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1, "MN1": mt5.TIMEFRAME_MN1,
}
MAX_BARS_PER_REQUEST = 100_000

_creds: Optional[dict] = None  # /connect 下发的账户, 优先于 env


def _env_creds() -> Optional[dict]:
    login = os.getenv("MT5_LOGIN", "").strip()
    if not login:
        return None
    return {"login": int(login), "password": os.getenv("MT5_PASSWORD", ""),
            "server": os.getenv("MT5_SERVER", "")}


def _connect() -> bool:
    return mt5_conn.attach(_creds or _env_creds())


def _reconnect_loop():
    """每 30s 检查连接; 掉了就重新附着。绝不拉终端/杀终端 —— 终端由人/开机自启开着。"""
    while True:
        time.sleep(30)
        if mt5_conn.is_alive():
            continue
        mt5_conn.drop()
        logger.warning("MT5 not connected, re-attaching (is the terminal open & logged in?)")
        if _connect():
            _, acc = mt5_conn.snapshot()
            logger.info("MT5 re-attached: login=%s server=%s", acc["login"], acc["server"])
        else:
            logger.warning("attach failed: %s", mt5_conn.last_error())


def _announce_loop():
    """周期性向 api 自报家门, Workers 页无需手动添加。新机器以 download 职能入册。"""
    if not DOCKER_COMPOSE_HOST or DOCKER_COMPOSE_HOST.startswith("127."):
        logger.warning("DOCKER_COMPOSE_HOST not set, skip auto-register")
        return
    api_base = f"http://{DOCKER_COMPOSE_HOST}:{API_PORT}"
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((DOCKER_COMPOSE_HOST, 1))
            my_ip = s.getsockname()[0]
            s.close()
            r = requests.post(f"{api_base}/hosts/announce", timeout=10,
                              json={"name": f"win-{my_ip.replace('.', '-')}",
                                    "host": my_ip, "port": BRIDGE_PORT})
            if r.status_code != 200:
                logger.warning("announce rejected: %s %s", r.status_code, r.text[:100])
        except Exception as e:
            logger.warning("announce failed (api not up yet?): %s", e)
        time.sleep(60)


def _startup_connect():
    if _connect():
        _, acc = mt5_conn.snapshot()
        logger.info("MT5 attached: login=%s server=%s balance=%s",
                    acc["login"], acc["server"], acc["balance"])
    else:
        logger.warning("MT5 not attached yet (open the terminal & log in; will keep retrying). "
                       "last_error=%s", mt5_conn.last_error())


def _require_key(x_api_key: Optional[str]):
    if BRIDGE_API_KEY and x_api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def _require_connected():
    if not mt5_conn.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")


app = FastAPI(title="MT5 Bridge", version="3.0.0")


@app.on_event("startup")
def startup():
    # 后台线程做首连: initialize 可能挂起十几秒, 不能阻塞 uvicorn 起监听
    threading.Thread(target=_startup_connect, daemon=True).start()
    threading.Thread(target=_reconnect_loop, daemon=True).start()
    threading.Thread(target=_announce_loop, daemon=True).start()


def _runner_status() -> dict:
    """读 runner 落盘的心跳; 60 秒没更新即视为没在跑"""
    try:
        data = json.loads(RUNNER_STATUS_FILE.read_text())
        data["alive"] = time.time() - data.get("updated", 0) < 60
        return data
    except (OSError, ValueError):
        return {"alive": False}


@app.get("/", response_class=HTMLResponse)
def status_page():
    """本机状态页: 浏览器打开 http://<本机>:8020/ 看全部服务"""
    up, account = mt5_conn.snapshot()
    mt5_up = up and account is not None
    runner = _runner_status()

    def badge(ok, text_ok, text_bad):
        style = ("color:#15803d;background:#ecfdf3;border:1px solid #bbf7d0" if ok
                 else "color:#b91c1c;background:#fef2f2;border:1px solid #fecaca")
        dot = "background:#15803d" if ok else "background:#b91c1c"
        return (f'<span style="display:inline-flex;align-items:center;gap:5px;padding:2px 10px;'
                f'border-radius:999px;font-size:12px;font-weight:550;{style}">'
                f'<i style="width:6px;height:6px;border-radius:50%;{dot}"></i>'
                f'{text_ok if ok else text_bad}</span>')

    rows = [
        ("bridge", badge(True, "运行中", ""), f"端口 {BRIDGE_PORT}"),
        ("MT5 终端", badge(mt5_up, "已连接", "未连接"),
         f"交易许可: {'是' if account and account.get('trade_allowed') else '—'}"),
        ("MT5 账户", badge(account is not None,
                          f"{account['login']} @ {account['server']}" if account else "", "未登录"),
         f"余额 {account['balance']:,.2f} {account['currency']}" if account
         else "开着 MT5 并登录 demo 账户即可"),
        ("runner", badge(runner["alive"], "运行中", "未运行"),
         f"角色 {runner.get('run_status', '—')} · 策略 {runner.get('strategies', '—')} 个"
         if runner["alive"] else "无角色时空转不连 MT5 (正常)"),
    ]
    trs = "".join(
        f'<tr><td style="padding:11px 14px;border-bottom:1px solid #e5e8ec;font-weight:550">{a}</td>'
        f'<td style="padding:11px 14px;border-bottom:1px solid #e5e8ec">{b}</td>'
        f'<td style="padding:11px 14px;border-bottom:1px solid #e5e8ec;color:#6b7280">{c}</td></tr>'
        for a, b, c in rows)
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MT5 Worker</title></head>
<body style="margin:0;background:#f6f7f9;font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#1a202c">
<div style="max-width:680px;margin:48px auto;padding:0 20px">
  <div style="font-weight:650;font-size:15px;margin-bottom:14px">
    <span style="color:#2563eb">&#9670;</span> MT5 Worker
    <span style="color:#9ca3af;font-weight:400;font-size:12px;margin-left:8px">10 秒自动刷新</span></div>
  <div style="background:#fff;border:1px solid #e5e8ec;border-radius:10px;box-shadow:0 1px 2px rgba(16,24,40,.04);overflow:hidden">
    <table style="border-collapse:collapse;width:100%">{trs}</table></div>
  <p style="color:#9ca3af;font-size:12px">JSON: <a href="/health" style="color:#2563eb">/health</a></p>
</div></body></html>"""


@app.get("/health")
def health():
    """心跳(无鉴权): app 只轮询这一个端点, 汇总 bridge/MT5/runner 状态"""
    up, account = mt5_conn.snapshot()
    mt5_up = up and account is not None
    runner = _runner_status()
    services = {
        "bridge": {"up": True, "port": BRIDGE_PORT},
        "mt5_terminal": {"up": mt5_up, "port": None},
        "runner": {"up": runner["alive"], "port": None},
    }
    summary = {"services_total": len(services),
               "services_up": sum(1 for s in services.values() if s["up"])}
    if not mt5_up:
        return {"status": "degraded", "mt5_connected": False, "runner": runner,
                "services": services, "summary": summary}
    return {"status": "healthy", "mt5_connected": True,
            "trade_allowed": account["trade_allowed"], "login": account["login"],
            "server": account["server"], "currency": account["currency"],
            "runner": runner, "services": services, "summary": summary}


class ConnectRequest(BaseModel):
    login: int
    password: str
    server: str


@app.post("/connect")
def connect(req: ConnectRequest, x_api_key: Optional[str] = Header(default=None)):
    """app 远程下发 MT5 账户并登录"""
    global _creds
    _require_key(x_api_key)
    _creds = {"login": req.login, "password": req.password, "server": req.server}
    if not _connect():
        _creds = None
        raise HTTPException(status_code=401, detail=f"MT5 login failed: {mt5_conn.last_error()}")
    return health()


@app.get("/account")
def account(x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    _require_connected()
    _, acc = mt5_conn.snapshot()
    if acc is None:
        raise HTTPException(status_code=500, detail="account_info failed")
    return acc


@app.get("/symbols")
def symbols(x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    _require_connected()
    with mt5_conn.lock():
        result = mt5.symbols_get()
    return {"symbols": [s.name for s in (result or [])]}


@app.get("/symbol/{symbol}")
def symbol_info(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    _require_connected()
    with mt5_conn.lock():
        if not mt5.symbol_select(symbol, True):
            raise HTTPException(status_code=404, detail=f"symbol {symbol} not found")
        info = mt5.symbol_info(symbol)
    if info is None:
        raise HTTPException(status_code=404, detail=f"symbol {symbol} not found")
    return info._asdict()


@app.get("/rates")
def rates(symbol: str, timeframe: str = "M1",
          from_ts: int = Query(...), to_ts: int = Query(...),
          x_api_key: Optional[str] = Header(default=None)):
    """按时间范围取K线, 供 app 下载器分页拉取"""
    _require_key(x_api_key)
    _require_connected()
    tf = TIMEFRAMES.get(timeframe.upper())
    if tf is None:
        raise HTTPException(status_code=400, detail=f"invalid timeframe: {timeframe}")
    if to_ts <= from_ts:
        raise HTTPException(status_code=400, detail="to_ts must be > from_ts")

    dt_from = datetime.fromtimestamp(from_ts, tz=timezone.utc)
    dt_to = datetime.fromtimestamp(to_ts, tz=timezone.utc)
    with mt5_conn.lock():
        mt5.symbol_select(symbol, True)
        data = mt5.copy_rates_range(symbol, tf, dt_from, dt_to)
        err = mt5.last_error() if data is None else None
    if data is None:
        raise HTTPException(status_code=500, detail=f"copy_rates_range failed: {err}")
    if len(data) > MAX_BARS_PER_REQUEST:
        raise HTTPException(status_code=413, detail=f"range too large ({len(data)} bars)")

    bars = [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "tick_volume": int(r["tick_volume"]), "spread": int(r["spread"]),
             "real_volume": int(r["real_volume"])} for r in data]
    return {"symbol": symbol, "timeframe": timeframe.upper(), "count": len(bars), "bars": bars}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)
