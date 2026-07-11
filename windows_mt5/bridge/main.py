"""MT5 Bridge - Windows worker HTTP API (端口 = env 的 MT5_PORT, 默认 8020)

哑执行器: 只负责 MT5 <-> HTTP 的转换, 不含业务逻辑。
MT5 账户三种来源(优先级由高到低):
  1. api 远程下发: POST /connect
  2. env/.dev.env 手动配置 MT5_LOGIN/PASSWORD/SERVER
  3. 都没有: 附着到本机已登录的 MT5 终端
"""
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
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
SELFTEST_FILE = Path(__file__).resolve().parents[1] / "selftest_result.json"  # 开机自检结果
# 终端启动配置: 固化"算法交易"等终端级开关 (克隆/重装的新机免手工点按钮)。
# 只在 bridge 拉起终端时生效; 手动双击打开的终端用它自己保存的设置。
TERMINAL_START_INI = Path(__file__).resolve().parent / "terminal_start.ini"
UPDATE_LOG = Path(__file__).resolve().parents[1] / "update_log.txt"  # 远程更新/重启的输出
REPO_ROOT = Path(__file__).resolve().parents[2]


def _git_version() -> str:
    """当前代码版本(git 短哈希) — 远程更新后核对版本号变没变, 就是成功凭证"""
    try:
        r = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "?"
    except OSError:
        return "?"


VERSION = _git_version()  # 启动时取一次即可: 代码变了必然经过重启

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
)
logger = logging.getLogger("bridge")

# 只到 D1: 系统只下载 M1(高周期聚合派生), 策略只做 M1~D1 bar; W1/MN1 无人用故不列
TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}
MAX_BARS_PER_REQUEST = 100_000

# MetaTrader5 包非线程安全: 所有 mt5 调用串行化
_mt5_lock = threading.Lock()
_connected = False
_creds: Optional[dict] = None  # /connect 下发的账户, 优先于 env
_account_cache: Optional[dict] = None  # 最近一次成功读到的账户信息, 供锁被长占时应答
_fail_streak = 0  # 连续连接失败次数, 满 6 次触发自愈(杀终端重拉)


def _env_creds() -> Optional[dict]:
    login = os.getenv("MT5_LOGIN", "").strip()
    if not login:
        return None
    return {"login": int(login), "password": os.getenv("MT5_PASSWORD", ""),
            "server": os.getenv("MT5_SERVER", "")}


def _terminal_count() -> int:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.count("terminal64.exe")
    except OSError:
        return -1  # 查不到进程表(罕见), 与 0 区分


def _terminal_running() -> bool:
    return _terminal_count() > 0


def _diag(procs: int) -> str:
    """连接失败时自动打环境诊断, 免人工逐项排查"""
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        elevated = "?"
    return (f"python_elevated={elevated} terminal_procs={procs} "
            f"mt5_pkg={getattr(mt5, '__version__', '?')} python={sys.executable}")


def _explain_failure(err: tuple, procs: int, streak: int) -> str:
    """把 -10005 这类哑巴错误码翻译成明确原因 + 系统即将采取的动作
    (日志一律英文: Windows 控制台默认 GBK 代码页, 中文会变乱码)"""
    if procs == 0:
        return "cause: no terminal64.exe process (launch failed or crashed) -> will relaunch on next retry"
    if procs > 1:
        return f"cause: {procs} terminal64.exe processes interfering -> self-heal will kill all and relaunch"
    if err and err[0] == -10005:
        return (f"cause: terminal process alive, permissions OK, but it ignores the IPC handshake"
                f" = this terminal instance is dead inside -> fail {streak}/6, at 6 it gets killed and relaunched")
    return f"cause: terminal rejected initialization (error code {err[0] if err else '?'})"


def _connect() -> bool:
    global _connected, _fail_streak
    creds = _creds or _env_creds()
    kwargs = dict(creds) if creds else {}
    if MT5_PATH:
        kwargs["path"] = MT5_PATH
        # 终端不在时用 Popen 显式拉起(等价于用户双击, 实测这样起的终端能连),
        # 不交给 initialize 隐式拉起(实测隐式拉起的终端 IPC 附着不上);
        # 拉起后等进程出现 + 冷启动缓冲, 再握手
        if not _terminal_running():
            logger.info("MT5 terminal not running, launching %s", MT5_PATH)
            args = [MT5_PATH]
            if TERMINAL_START_INI.exists():  # 自动开启算法交易等开关
                args.append(f"/config:{TERMINAL_START_INI}")
            try:
                subprocess.Popen(args, cwd=str(Path(MT5_PATH).parent))
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
            _fail_streak = 0  # IPC 已通, 终端是好的 - 没登录账户也不该触发杀终端自愈
            info = mt5.account_info()
            if info is None:
                _connected = False
                logger.error("MT5 IPC OK but no account logged in -> push an account from the "
                             "web Workers page, or set MT5_LOGIN/PASSWORD/SERVER in env\\.dev.env and restart")
                return False
            _connected = True
            logger.info("MT5 connected: login=%s server=%s balance=%s",
                        info.login, info.server, info.balance)
        else:
            _connected = False
            _fail_streak += 1
            err = mt5.last_error()
            procs = _terminal_count()
            logger.error("MT5 connect failed %s | %s | %s",
                         err, _explain_failure(err, procs, _fail_streak), _diag(procs))
    return _connected


def _reconnect_loop():
    global _connected, _fail_streak
    while True:
        time.sleep(30)
        with _mt5_lock:
            alive = _connected and mt5.terminal_info() is not None
        if alive:
            continue
        _connected = False
        # 自愈: 连续多次附着不上 = 终端实例已僵死(实测存在这种僵尸实例),
        # 杀掉重拉 - _connect 发现终端不在会用 Popen 重新拉起一个干净的
        if _fail_streak >= 6 and MT5_PATH:
            logger.warning("self-heal: %d connect failures in a row, killing the dead terminal and relaunching",
                           _fail_streak)
            subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"],
                           capture_output=True, timeout=15)
            time.sleep(5)
            _fail_streak = 0
        logger.warning("MT5 not connected, retrying...")
        _connect()


def _announce_loop():
    """自动注册: 周期性向 api 自报家门, Workers 页面无需手动添加。
    新机器以 download 角色入册, demo/live 由人在 web 上指派。"""
    if not DOCKER_COMPOSE_HOST or DOCKER_COMPOSE_HOST.startswith("127."):
        logger.warning("DOCKER_COMPOSE_HOST not set, skip auto-register (register manually on the web Workers page)")
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
            logger.warning("announce failed (api not up yet?): %s", e)
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


def _selftest() -> Optional[dict]:
    """读开机自检结果 (selftest.py 写盘) — 检查项只在 selftest.py 一处定义,
    这里和 /health 只做搬运: 状态页/api心跳/web 全部消费同一份数据"""
    try:
        return json.loads(SELFTEST_FILE.read_text())
    except (OSError, ValueError):
        return None


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

    st = _selftest()
    if st:
        fails = [c["name"] for c in st["checks"] if c["status"] == "FAIL"]
        n_pass = sum(1 for c in st["checks"] if c["status"] == "PASS")
        st_badge = badge(st["ok"], f"OK {n_pass}/{len(st['checks'])}",
                         "FAIL: " + ", ".join(fails))
        st_note = datetime.fromtimestamp(st["updated"]).strftime("%m-%d %H:%M") + " · 重跑: selftest.bat"
    else:
        st_badge, st_note = badge(False, "", "未运行"), "双击 selftest.bat 或重启机器"

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
        ("开机自检", st_badge, st_note),
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
    <span style="color:#9ca3af;font-weight:400;font-size:12px;margin-left:8px">版本 {VERSION} · 10 秒自动刷新</span>
  </div>
  <div style="background:#fff;border:1px solid #e5e8ec;border-radius:10px;box-shadow:0 1px 2px rgba(16,24,40,.04);overflow:hidden">
    <table style="border-collapse:collapse;width:100%">{trs}</table>
  </div>
  <p style="color:#9ca3af;font-size:12px">JSON: <a href="/health" style="color:#2563eb">/health</a>
   · 调试: <a href="/trades?fmt=html" style="color:#2563eb">交易流水</a>
   <a href="/recon" style="color:#2563eb">交易对账</a>
   <button onclick="ordertest()" style="font-size:12px;cursor:pointer">下单测试 (仅demo)</button></p>
<script>
async function ordertest() {{
  if (!confirm("在 DEMO 账户开一笔最小单并立即平掉 (成本一个点差)?")) return;
  try {{
    const r = await fetch("/ordertest", {{method: "POST"}});
    alert(JSON.stringify(await r.json(), null, 2));
  }} catch (e) {{ alert("请求失败: " + e); }}
}}
</script>
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
                "selftest": _selftest(), "version": VERSION,
                "services": services, "summary": summary}
    return {
        "status": "healthy",
        "version": VERSION,
        "mt5_connected": True,
        "trade_allowed": account["trade_allowed"],
        "login": account["login"],
        "server": account["server"],
        "currency": account["currency"],
        "runner": runner,
        "selftest": _selftest(),
        "services": services,
        "summary": summary,
    }


def _spawn_maintenance(script: str) -> None:
    """分离进程跑 update.ps1/restart.ps1: 脚本会 taskkill 所有 python(含本 bridge),
    powershell 不是 python 所以存活, 完成 pull/重启后看门狗+自检自动接管。
    输出追加到 update_log.txt (版本号没变时来这里查原因)。"""
    ps1 = Path(__file__).resolve().parents[1] / script
    cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{ps1}" >> "{UPDATE_LOG}" 2>&1'
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["cmd", "/c", cmd], cwd=str(ps1.parent), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.post("/update")
def remote_update(x_api_key: Optional[str] = Header(default=None)):
    """远程更新: git pull + 依赖 + 重启 + 自检 (逻辑全在 update.ps1, 这里只触发)"""
    _require_key(x_api_key)
    logger.info("remote update triggered (version %s)", VERSION)
    _spawn_maintenance("update.ps1")
    return {"started": True, "from_version": VERSION,
            "note": "worker 将离线约1分钟; 回来后核对 version 变化 + 自检 OK"}


@app.post("/restart")
def remote_restart(x_api_key: Optional[str] = Header(default=None)):
    """远程重启服务 (不更新代码; 逻辑全在 restart.ps1, 这里只触发)"""
    _require_key(x_api_key)
    logger.info("remote restart triggered")
    _spawn_maintenance("restart.ps1")
    return {"started": True, "note": "worker 将离线约1分钟, 自检自动重跑"}


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


_DEAL_TYPE = {0: "buy", 1: "sell", 2: "balance"}
_DEAL_ENTRY = {0: "in", 1: "out", 2: "inout", 3: "out_by"}
_DEAL_REASON = {0: "manual", 1: "mobile", 2: "web", 3: "expert", 4: "sl", 5: "tp", 6: "so"}


def _trades_data(days: int) -> dict:
    now = datetime.now(timezone.utc)
    with _mt5_lock:  # +1天缓冲: 历史过滤按券商服务器时间
        positions = mt5.positions_get() or []
        deals = mt5.history_deals_get(now - timedelta(days=days), now + timedelta(days=1)) or []
    return {
        "days": days,
        "positions": [{
            "ticket": p.ticket, "time": p.time, "symbol": p.symbol,
            "type": _DEAL_TYPE.get(p.type, str(p.type)), "volume": p.volume,
            "price_open": p.price_open, "sl": p.sl, "tp": p.tp,
            "price_current": p.price_current, "profit": p.profit, "swap": p.swap,
            "magic": p.magic, "comment": p.comment,
        } for p in positions],
        "deals": [{
            "ticket": d.ticket, "position_id": d.position_id, "time": d.time,
            "symbol": d.symbol, "type": _DEAL_TYPE.get(d.type, str(d.type)),
            "entry": _DEAL_ENTRY.get(d.entry, str(d.entry)),
            "reason": _DEAL_REASON.get(d.reason, str(d.reason)),
            "volume": d.volume, "price": d.price, "profit": d.profit,
            "commission": d.commission, "swap": d.swap,
            "magic": d.magic, "comment": d.comment,
        } for d in sorted(deals, key=lambda d: -d.time)],
    }


@app.get("/trades")
def trades(days: int = 30, fmt: str = "json",
           x_api_key: Optional[str] = Header(default=None)):
    """交易流水(只读): 当前持仓 + 历史成交明细, 原样透传 MT5。
    json 给 api/web /mt5 页用(带鉴权); fmt=html 本机浏览器直接看(与状态页同级, 免鉴权即免登录)。
    时间是 epoch 秒(券商服务器时钟); deals 按时间倒序。"""
    if fmt != "html":
        _require_key(x_api_key)
    _require_connected()
    data = _trades_data(days)
    if fmt != "html":
        return data

    def ts(t):
        return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S")

    pos_rows = "".join(
        f"<tr><td>{p['ticket']}</td><td>{ts(p['time'])}</td><td>{p['symbol']}</td>"
        f"<td>{p['type']}</td><td>{p['volume']}</td><td>{p['price_open']}</td>"
        f"<td>{p['sl']}</td><td>{p['tp']}</td><td>{p['price_current']}</td>"
        f"<td style='text-align:right'>{p['profit']:+.2f}</td><td>{p['magic']}</td>"
        f"<td>{_magic_note(p['magic'])}</td></tr>" for p in data["positions"]) \
        or "<tr><td colspan=12>无持仓</td></tr>"
    deal_rows = "".join(
        f"<tr><td>{ts(d['time'])}</td><td>{d['ticket']}</td><td>{d['position_id']}</td>"
        f"<td>{d['symbol'] or '—'}</td><td>{d['type']}</td><td>{d['entry']}</td>"
        f"<td>{d['reason']}</td><td>{d['volume']}</td><td>{d['price']}</td>"
        f"<td style='text-align:right'>{d['profit']:+.2f}</td><td>{d['commission']}</td>"
        f"<td>{d['swap']}</td><td>{d['magic']}</td><td>{_magic_note(d['magic'])}</td></tr>"
        for d in data["deals"]) or "<tr><td colspan=14>无成交</td></tr>"
    return HTMLResponse(f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>交易流水</title><style>td,th{{border:1px solid #d7dbe0;padding:4px 10px;font:12px/1.5 sans-serif;white-space:nowrap}}
th{{background:#f1f5f9}}body{{font:14px/1.6 sans-serif;margin:32px;color:#1a202c}}table{{border-collapse:collapse;margin:8px 0 20px}}</style></head><body>
<h2>交易流水 (近 {days} 天, MT5 原样) <a href="/" style="font-size:12px">← 状态页</a>
 <a href="/recon" style="font-size:12px">对账</a></h2>
<p style="color:#6b7280;font-size:12px">时间为券商服务器时间; entry: in=开仓腿 out=平仓腿; reason: sl/tp=止损止盈触发 expert=程序下单 manual=手动</p>
<h3>当前持仓 ({len(data['positions'])})</h3>
<table><tr><th>ticket</th><th>开仓时间</th><th>品种</th><th>方向</th><th>手数</th><th>开仓价</th>
<th>SL</th><th>TP</th><th>现价</th><th>浮动</th><th>magic</th><th>归属</th></tr>{pos_rows}</table>
<h3>历史成交 ({len(data['deals'])})</h3>
<table><tr><th>时间</th><th>ticket</th><th>仓位ID</th><th>品种</th><th>类型</th><th>腿</th><th>原因</th>
<th>手数</th><th>价格</th><th>盈亏</th><th>手续费</th><th>swap</th><th>magic</th><th>归属</th></tr>{deal_rows}</table>
</body></html>""")


# ---------- 调试端点 (浏览器直接用, 无需登录 Windows; diag/*.bat 保留作 bridge 挂掉时的兜底) ----------
SMOKE_MAGIC = 999999  # 冒烟测试专用 magic, 永不与策略(100000+id)冲突, web 战绩不统计它


def _magic_note(magic: int) -> str:
    if magic == 0:
        return "手动/非策略"
    if magic == SMOKE_MAGIC:
        return "下单测试"
    if 100_000 <= magic < 200_000:
        return f"策略 #{magic - 100_000}"
    return "?"


@app.get("/recon")
def recon(days: int = 90, fmt: str = "html"):
    """交易对账(只读): 近 N 天成交按 magic 分组, 与 web Demo/Live 页战绩逐行对应。
    web笔数 = out 列; web已实现盈亏 = pnl 列 (profit+手续费+隔夜利息, 与 conn/stats 同口径)。
    RAW COUNTS 解释 MT5 历史标签三种视图各显示多少行 — 对不上数字先看这里。"""
    _require_connected()
    now = datetime.now(timezone.utc)
    with _mt5_lock:  # +1天缓冲: 历史过滤按券商服务器时间, 与 UTC 偏差以小时计
        deals = mt5.history_deals_get(now - timedelta(days=days), now + timedelta(days=1)) or []
        orders = mt5.history_orders_get(now - timedelta(days=days), now + timedelta(days=1)) or []
        positions = mt5.positions_get() or []

    by_magic, balance_rows = {}, 0
    for d in deals:
        if d.type == mt5.DEAL_TYPE_BALANCE:
            balance_rows += 1
            continue
        s = by_magic.setdefault(d.magic, {"in": 0, "out": 0, "wins": 0, "pnl": 0.0})
        if d.entry == mt5.DEAL_ENTRY_IN:
            s["in"] += 1
        elif d.entry == mt5.DEAL_ENTRY_OUT:  # 平仓腿: 盈亏落在这条上 (与 web 同口径)
            s["out"] += 1
            pnl = d.profit + d.commission + d.swap
            s["pnl"] += pnl
            if pnl > 0:
                s["wins"] += 1
    closed = [{"magic": m, **{k: round(v, 2) if k == "pnl" else v for k, v in s.items()},
               "note": _magic_note(m)} for m, s in sorted(by_magic.items())]
    open_pos = {}
    for p in positions:
        o = open_pos.setdefault(p.magic, {"count": 0, "volume": 0.0, "profit": 0.0})
        o["count"] += 1
        o["volume"] = round(o["volume"] + p.volume, 2)
        o["profit"] = round(o["profit"] + p.profit, 2)
    data = {
        "days": days,
        "closed_by_magic": closed,
        "strategy_totals": {  # web 页面各列加总必须等于这两个数
            "closed": sum(s["out"] for m, s in by_magic.items() if 100_000 <= m < 200_000),
            "realized": round(sum(s["pnl"] for m, s in by_magic.items()
                                  if 100_000 <= m < 200_000), 2),
        },
        "open_positions": [{"magic": m, **o, "note": _magic_note(m)}
                           for m, o in sorted(open_pos.items())],
        "raw_counts": {
            "positions_view": sum(s["out"] for s in by_magic.values()),
            "orders_view": len(orders),
            "deals_view": len(deals),
            "balance_rows": balance_rows,
            "open_now": len(positions),
        },
    }
    if fmt == "json":
        return data
    rows = "".join(
        f"<tr><td>{c['magic']}</td><td>{c['in']}</td><td>{c['out']}</td><td>{c['wins']}</td>"
        f"<td style='text-align:right'>{c['pnl']:+.2f}</td><td>{c['note']}</td></tr>" for c in closed)
    op = "".join(
        f"<tr><td>{o['magic']}</td><td>{o['count']} 仓</td><td>{o['volume']} 手</td>"
        f"<td style='text-align:right'>{o['profit']:+.2f}</td><td>{o['note']}</td></tr>"
        for o in data["open_positions"]) or "<tr><td colspan=5>无持仓</td></tr>"
    rc = data["raw_counts"]
    t = data["strategy_totals"]
    css = "border-collapse:collapse;margin:8px 0 20px"
    return HTMLResponse(f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>交易对账</title><style>td,th{{border:1px solid #d7dbe0;padding:5px 12px;font:13px/1.5 sans-serif}}
th{{background:#f1f5f9}}body{{font:14px/1.6 sans-serif;margin:32px;color:#1a202c}}</style></head><body>
<h2>交易对账 (近 {days} 天, 只读) <a href="/recon?fmt=json" style="font-size:12px">JSON</a>
 <a href="/" style="font-size:12px">← 状态页</a></h2>
<h3>已平仓 (按 magic — 与 web 页面"笔数/已实现盈亏"逐行对应)</h3>
<table style="{css}"><tr><th>magic</th><th>开仓腿</th><th>平仓腿=web笔数</th><th>胜场</th><th>盈亏=web已实现</th><th>归属</th></tr>{rows}</table>
<b>策略合计: {t['closed']} 笔 / {t['realized']:+.2f}</b> — web 页面各列加总必须等于这两个数<br>
<h3>当前持仓 (web 显示在"持仓/浮动盈亏", 不计入笔数)</h3>
<table style="{css}"><tr><th>magic</th><th>仓数</th><th>手数</th><th>浮动</th><th>归属</th></tr>{op}</table>
<h3>MT5 历史标签为什么对不上 — 三种视图行数</h3>
<ul><li>仓位视图: {rc['positions_view']} 行 (每笔平仓 1 行)</li>
<li>订单视图: {rc['orders_view']} 行 (每仓开+平 2 行)</li>
<li>成交视图: {rc['deals_view']} 行 (含 {rc['balance_rows']} 行入金/出金)</li>
<li>当前持仓 {rc['open_now']} 个在"交易"标签, 永远不在历史里</li></ul>
</body></html>""")


@app.post("/ordertest")
def ordertest(symbol: str = "XAUUSD"):
    """下单链路冒烟测试: 与 runner send_order 完全相同的请求结构开一笔最小单并立即平掉。
    硬保护: 只允许 DEMO 账户 (live 主机上直接拒绝), 成本一个点差。"""
    _require_connected()
    with _mt5_lock:
        acct = mt5.account_info()
        if acct is None or acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise HTTPException(status_code=403, detail="仅限 DEMO 账户 — 真实账户拒绝测试下单")
        term = mt5.terminal_info()
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol) if info else None
        if info is None or tick is None or tick.ask == 0:
            raise HTTPException(status_code=400, detail=f"{symbol} 无报价 (品种名不对或休市)")
        if not term.trade_allowed:
            raise HTTPException(status_code=400, detail="算法交易开关未开 (工具栏 Algo Trading)")
        volume = max(info.volume_min, 0.01)
        dist = max(info.trade_stops_level * 3, 500) * info.point
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
               "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
               "sl": tick.ask - dist, "tp": tick.ask + dist, "deviation": 20,
               "magic": SMOKE_MAGIC, "comment": "bridge-ordertest",
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
        r = mt5.order_send(req)
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(status_code=502, detail={
                "open": "rejected", "retcode": r.retcode if r else None,
                "comment": r.comment if r else str(mt5.last_error())})
        opened = {"ticket": r.order, "price": r.price, "volume": volume}
        pos = next((p for p in (mt5.positions_get(symbol=symbol) or [])
                    if p.magic == SMOKE_MAGIC), None)
        if pos is None:
            return {"result": "PARTIAL", "open": opened,
                    "close": "position not found - close it manually in MT5"}
        tick = mt5.symbol_info_tick(symbol)
        c = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                            "volume": pos.volume, "type": mt5.ORDER_TYPE_SELL,
                            "price": tick.bid, "deviation": 20, "magic": SMOKE_MAGIC,
                            "position": pos.ticket, "comment": "ordertest-close",
                            "type_time": mt5.ORDER_TIME_GTC,
                            "type_filling": mt5.ORDER_FILLING_IOC})
    if c is None or c.retcode != mt5.TRADE_RETCODE_DONE:
        return {"result": "PARTIAL", "open": opened,
                "close": f"failed retcode={c.retcode if c else None} - close manually in MT5"}
    logger.info("ordertest PASS: open %.5f close %.5f", opened["price"], c.price)
    return {"result": "PASS", "open": opened, "close": {"price": c.price},
            "note": "full order path works; cost = one spread"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)
