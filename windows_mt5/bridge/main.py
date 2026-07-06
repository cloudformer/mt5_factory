"""MT5 Bridge - Windows worker HTTP API (端口固定 9090)

哑执行器: 只负责 MT5 <-> HTTP 的转换, 不含业务逻辑。
MT5 账户三种来源(优先级由高到低):
  1. app 远程下发: POST /connect
  2. worker.env 手动配置 MT5_LOGIN/PASSWORD/SERVER
  3. 都没有: 附着到本机已登录的 MT5 终端
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent.parent / "worker.env")

BRIDGE_PORT = 9090  # 固定端口, 防火墙/注册表都按它来
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")

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

# MetaTrader5 包非线程安全: 所有 mt5 调用串行化
_mt5_lock = threading.Lock()
_connected = False
_creds: Optional[dict] = None  # /connect 下发的账户, 优先于 env


def _env_creds() -> Optional[dict]:
    login = os.getenv("MT5_LOGIN", "").strip()
    if not login:
        return None
    return {"login": int(login), "password": os.getenv("MT5_PASSWORD", ""),
            "server": os.getenv("MT5_SERVER", "")}


def _connect() -> bool:
    global _connected
    creds = _creds or _env_creds()
    with _mt5_lock:
        mt5.shutdown()
        ok = mt5.initialize(**creds) if creds else mt5.initialize()
        if ok:
            info = mt5.account_info()
            if info is None:
                _connected = False
                logger.error("MT5 initialized but no account logged in")
                return False
            _connected = True
            logger.info("MT5 connected: login=%s server=%s balance=%s",
                        info.login, info.server, info.balance)
        else:
            _connected = False
            logger.error("MT5 initialize failed: %s", mt5.last_error())
    return _connected


def _reconnect_loop():
    global _connected
    while True:
        time.sleep(30)
        with _mt5_lock:
            alive = _connected and mt5.terminal_info() is not None
        if not alive:
            _connected = False
            logger.warning("MT5 disconnected, reconnecting...")
            _connect()


def _require_key(x_api_key: Optional[str]):
    if BRIDGE_API_KEY and x_api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def _require_connected():
    if not _connected:
        raise HTTPException(status_code=503, detail="MT5 not connected")


app = FastAPI(title="MT5 Bridge", version="2.0.0")


@app.on_event("startup")
def startup():
    _connect()  # 失败不退出: 重连守护接管, /health 如实上报
    threading.Thread(target=_reconnect_loop, daemon=True).start()


@app.get("/health")
def health():
    """心跳端点(无鉴权): app 轮询更新 mt5_hosts.last_heartbeat"""
    if not _connected:
        return {"status": "degraded", "mt5_connected": False}
    with _mt5_lock:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
    if terminal is None or account is None:
        return {"status": "degraded", "mt5_connected": False}
    return {
        "status": "healthy",
        "mt5_connected": True,
        "trade_allowed": terminal.trade_allowed,
        "login": account.login,
        "server": account.server,
        "currency": account.currency,
    }


class ConnectRequest(BaseModel):
    login: int
    password: str
    server: str


@app.post("/connect")
def connect(req: ConnectRequest, x_api_key: Optional[str] = Header(default=None)):
    """app 远程下发 MT5 账户并登录 (无需在 Windows 上手动配置)"""
    global _creds
    _require_key(x_api_key)
    _creds = {"login": req.login, "password": req.password, "server": req.server}
    if not _connect():
        with _mt5_lock:
            err = mt5.last_error()
        _creds = None
        raise HTTPException(status_code=401, detail=f"MT5 login failed: {err}")
    return health()


@app.get("/account")
def account(x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    _require_connected()
    with _mt5_lock:
        info = mt5.account_info()
    if info is None:
        raise HTTPException(status_code=500, detail="account_info failed")
    return info._asdict()


@app.get("/symbols")
def symbols(x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    _require_connected()
    with _mt5_lock:
        result = mt5.symbols_get()
    return {"symbols": [s.name for s in (result or [])]}


@app.get("/symbol/{symbol}")
def symbol_info(symbol: str, x_api_key: Optional[str] = Header(default=None)):
    _require_key(x_api_key)
    _require_connected()
    with _mt5_lock:
        if not mt5.symbol_select(symbol, True):
            raise HTTPException(status_code=404, detail=f"symbol {symbol} not found")
        info = mt5.symbol_info(symbol)
    if info is None:
        raise HTTPException(status_code=404, detail=f"symbol {symbol} not found")
    return info._asdict()


@app.get("/rates")
def rates(
    symbol: str,
    timeframe: str = "M1",
    from_ts: int = Query(..., description="起始时间(epoch秒, UTC)"),
    to_ts: int = Query(..., description="结束时间(epoch秒, UTC, 不含)"),
    x_api_key: Optional[str] = Header(default=None),
):
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
    with _mt5_lock:
        mt5.symbol_select(symbol, True)
        data = mt5.copy_rates_range(symbol, tf, dt_from, dt_to)

    if data is None:
        with _mt5_lock:
            err = mt5.last_error()
        raise HTTPException(status_code=500, detail=f"copy_rates_range failed: {err}")
    if len(data) > MAX_BARS_PER_REQUEST:
        raise HTTPException(status_code=413, detail=f"range too large ({len(data)} bars)")

    bars = [
        {
            "time": int(r["time"]),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "tick_volume": int(r["tick_volume"]),
            "spread": int(r["spread"]),
            "real_volume": int(r["real_volume"]),
        }
        for r in data
    ]
    return {"symbol": symbol, "timeframe": timeframe.upper(), "count": len(bars), "bars": bars}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)
