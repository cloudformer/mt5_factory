"""MT5 Bridge - Windows worker HTTP API (端口 = env 的 MT5_PORT, 默认 8020)

哑执行器: 只负责 MT5 <-> HTTP 的转换, 不含业务逻辑。
MT5 账户三种来源(优先级由高到低):
  1. api 远程下发: POST /connect
  2. env/.dev.env 手动配置 MT5_LOGIN/PASSWORD/SERVER
  3. 都没有: 附着到本机已登录的 MT5 终端
"""
import json
import logging
import os
import socket
import subprocess
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

# 统一配置: 与 Linux docker compose 共用 env/.dev.env (整仓 clone 到 Windows)
load_dotenv(Path(__file__).resolve().parents[2] / "env" / ".dev.env")

BRIDGE_PORT = int(os.getenv("MT5_PORT", "8020"))  # 与 api 注册 worker 的端口同源
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")
DOCKER_COMPOSE_HOST = os.getenv("DOCKER_COMPOSE_HOST", "").strip()
API_PORT = os.getenv("API_PORT", "8010")
# mt5.initialize() 不给 path 时的自动定位常失效 (报 "MetaTrader 5 x64 not found" 但其实已装),
# setup.ps1 探测到终端后会自动写入这个变量
MT5_PATH = os.getenv("MT5_PATH", "").strip()
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

# MetaTrader5 包非线程安全: 所有 mt5 调用串行化
_mt5_lock = threading.Lock()
_connected = False
_creds: Optional[dict] = None  # /connect 下发的账户, 优先于 env
_account_cache: Optional[dict] = None  # 最近一次成功读到的账户信息, 供锁被长占时应答


def _env_creds() -> Optional[dict]:
    login = os.getenv("MT5_LOGIN", "").strip()
    if not login:
        return None
    return {"login": int(login), "password": os.getenv("MT5_PASSWORD", ""),
            "server": os.getenv("MT5_SERVER", "")}


def _terminal_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
                             capture_output=True, text=True, timeout=10)
        return "terminal64.exe" in out.stdout
    except OSError:
        return False


def _connect() -> bool:
    global _connected
    creds = _creds or _env_creds()
    kwargs = dict(creds) if creds else {}
    if MT5_PATH:
        kwargs["path"] = MT5_PATH
        # 终端不在时用 Popen 显式拉起(等价于用户双击, 实测这样起的终端能连),
        # 不交给 initialize 隐式拉起(实测隐式拉起的终端 IPC 附着不上);
        # 拉起后等进程出现 + 冷启动缓冲, 再握手
        if not _terminal_running():
            logger.info("MT5 terminal not running, launching %s", MT5_PATH)
            try:
                subprocess.Popen([MT5_PATH], cwd=str(Path(MT5_PATH).parent))
            except OSError as e:
                logger.error("launch terminal failed: %s", e)
                return False
            deadline = time.time() + 60
            while time.time() < deadline and not _terminal_running():
                time.sleep(3)
            time.sleep(15)
    # initialize 挂起期间持有 GIL, 整个进程(含 /health)都会冻结 -
    # 默认 60s 超时太长, 15s 快败, 交给重连循环再试
    kwargs["timeout"] = 15_000
    with _mt5_lock:
        mt5.shutdown()
        ok = mt5.initialize(**kwargs)
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
            err = mt5.last_error()
            logger.error("MT5 initialize failed: %s", err)
            if err and err[0] == -10005:
                logger.error("IPC timeout 排查: 1) 终端和 python 权限要一致 (手动开终端时别用'管理员身份运行') "
                             "2) 任务管理器里确认只有一个 terminal64.exe (多开互相干扰, 全部关掉让 bridge 自动拉起) "
                             "3) 刚装的终端可能正在下载更新, 等 1-2 分钟会自动重连")
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


def _announce_loop():
    """自动注册: 周期性向 api 自报家门, Workers 页面无需手动添加。
    新机器以 download 角色入册, demo/live 由人在 web 上指派。"""
    if not DOCKER_COMPOSE_HOST or DOCKER_COMPOSE_HOST.startswith("127."):
        logger.warning("DOCKER_COMPOSE_HOST 未配置, 跳过自动注册 (可在 web Workers 页手动注册)")
        return
    api_base = f"http://{DOCKER_COMPOSE_HOST}:{API_PORT}"
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((DOCKER_COMPOSE_HOST, 1))
            my_ip = s.getsockname()[0]
            s.close()
            r = requests.post(f"{api_base}/hosts/announce", timeout=10, json={
                "name": f"win-{my_ip.replace('.', '-')}",
                "host": my_ip,
                "port": BRIDGE_PORT,
            })
            if r.status_code != 200:
                logger.warning("announce rejected: %s %s", r.status_code, r.text[:100])
        except Exception as e:
            logger.warning("announce failed (api 未就绪?): %s", e)
        time.sleep(60)


def _require_key(x_api_key: Optional[str]):
    if BRIDGE_API_KEY and x_api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def _require_connected():
    if not _connected:
        raise HTTPException(status_code=503, detail="MT5 not connected")


app = FastAPI(title="MT5 Bridge", version="2.0.0")


@app.on_event("startup")
def startup():
    # _connect() 必须放后台线程: uvicorn 等 startup 跑完才开始监听端口,
    # 而 mt5.initialize() 在终端卡弹窗/首启慢时会挂起几十秒甚至更久,
    # 同步调用会导致 8020 整个起不来, /health 不可达, 心跳误判离线
    threading.Thread(target=_connect, daemon=True).start()
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


def _mt5_snapshot() -> tuple:
    """(mt5是否在线, 账户信息dict或None) - 限时抢锁。
    initialize 挂起(IPC timeout 要 60s)或大批量拉 bars 时锁被长占, /health 若死等锁,
    app 心跳就超时误判 OFFLINE - 拿不到锁立即用缓存应答, 状态端点绝不阻塞"""
    global _account_cache
    if _mt5_lock.acquire(timeout=2):
        try:
            term = mt5.terminal_info() if _connected else None
            info = mt5.account_info() if _connected else None
            if term and info:
                _account_cache = {"login": info.login, "server": info.server,
                                  "currency": info.currency, "balance": info.balance,
                                  "trade_allowed": bool(term.trade_allowed)}
                return True, _account_cache
            return False, None
        finally:
            _mt5_lock.release()
    return _connected, _account_cache if _connected else None


@app.get("/", response_class=HTMLResponse)
def status_page():
    """本机状态页: 浏览器打开 http://<本机>:8020/ 看全部服务"""
    mt5_up, account = _mt5_snapshot()
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
         f"交易许可: {'是' if account and account['trade_allowed'] else '—'}"),
        ("MT5 账户", badge(account is not None,
                          f"{account['login']} @ {account['server']}" if account else "", "未登录"),
         f"余额 {account['balance']:,.2f} {account['currency']}" if account else "可在 web Workers 页下发账户"),
        ("runner", badge(runner["alive"], "运行中", "未运行"),
         f"角色 {runner.get('run_status', '—')} · 策略 {runner.get('strategies', '—')} 个"
         if runner["alive"] else "检查 start_runner.bat"),
    ]
    trs = "".join(
        f'<tr><td style="padding:11px 14px;border-bottom:1px solid #e5e8ec;font-weight:550">{a}</td>'
        f'<td style="padding:11px 14px;border-bottom:1px solid #e5e8ec">{b}</td>'
        f'<td style="padding:11px 14px;border-bottom:1px solid #e5e8ec;color:#6b7280">{c}</td></tr>'
        for a, b, c in rows)
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MT5 Worker</title></head>
<body style="margin:0;background:#f6f7f9;font:14px/1.6 -apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#1a202c">
<div style="max-width:680px;margin:48px auto;padding:0 20px">
  <div style="font-weight:650;font-size:15px;margin-bottom:14px">
    <span style="color:#2563eb">◆</span> MT5 Worker
    <span style="color:#9ca3af;font-weight:400;font-size:12px;margin-left:8px">10 秒自动刷新</span>
  </div>
  <div style="background:#fff;border:1px solid #e5e8ec;border-radius:10px;box-shadow:0 1px 2px rgba(16,24,40,.04);overflow:hidden">
    <table style="border-collapse:collapse;width:100%">{trs}</table>
  </div>
  <p style="color:#9ca3af;font-size:12px">JSON: <a href="/health" style="color:#2563eb">/health</a></p>
</div></body></html>"""


@app.get("/health")
def health():
    """心跳端点(无鉴权): app 只轮询这一个端点, 本机 bridge/MT5/runner 状态 + 服务/端口汇总
    在这里一次性收集齐, 不需要 app 再单独探测每个服务(runner 没有对外端口, 只能本机汇总)"""
    runner = _runner_status()
    up, account = _mt5_snapshot()
    mt5_up = up and account is not None

    services = {
        "bridge": {"up": True, "port": BRIDGE_PORT},
        "mt5_terminal": {"up": mt5_up, "port": None},
        "runner": {"up": runner["alive"], "port": None},
    }
    summary = {
        "services_total": len(services),
        "services_up": sum(1 for s in services.values() if s["up"]),
        "ports": {name: s["port"] for name, s in services.items() if s["port"]},
    }

    if not mt5_up:
        return {"status": "degraded", "mt5_connected": False, "runner": runner,
                "services": services, "summary": summary}
    return {
        "status": "healthy",
        "mt5_connected": True,
        "trade_allowed": account["trade_allowed"],
        "login": account["login"],
        "server": account["server"],
        "currency": account["currency"],
        "runner": runner,
        "services": services,
        "summary": summary,
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
