"""MT5 Bridge - Windows worker HTTP API (端口 = env 的 WINDOWS_BRIDGE_PORT, 默认 8020)

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
from urllib.parse import urlparse
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
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

# 统一配置: 与 Linux docker compose 共用 env/.dev.env (整仓 clone 到 Windows)
load_dotenv(Path(__file__).resolve().parents[2] / "env" / ".dev.env")

# WINDOWS_BRIDGE_PORT(2026-08-13 由 MT5_PORT 改名): 原名有歧义 —— MT5 终端本身跟这个端口无关, 它是 bridge 这个 HTTP 服务的端口
BRIDGE_PORT = int(os.getenv("WINDOWS_BRIDGE_PORT", "8020"))  # 与 api 注册 worker 的端口同源
# BRIDGE_API_KEY 已删(2026-08-13): 死代码清理, 不是安全改动。
# 它诞生于"api 会主动连 worker"的年代; 2026-07-26 v7.2 单向化把三个远程端点
# (/connect、远程重启、实时流水透传)全删了, Linux 侧至今零出站 HTTP 客户端 —
# 这把钥匙从此没有任何调用方带过。8020 的访问控制归 Windows 防火墙, 代码层不管。
# worker 钥匙(schema/040): announce 带上它 → api 认钥知主(host 自动归该用户+一机一钥首绑)。
# 未配置=照旧匿名注册(兼容期; v5.6 起强制)。它证明"我是谁"。
WORKER_KEY = os.getenv("WORKER_KEY", "").strip()
# Linux api 的完整地址(2026-08-13 取代 DOCKER_COMPOSE_HOST+API_PORT 的拼接):
# 一个变量说清协议+主机+端口+路径, 不再有"API_PORT 指哪个 API"的歧义;
# 跨公网的 worker 写 https:// 即可, 否则 WORKER_KEY 明文过网被抄走就能冒充本机。
#   内网 http://192.168.4.130:8010  |  公网 https://api.demo.com/api
SERVER_API_URL = os.getenv("SERVER_API_URL", "").strip().rstrip("/")
# 上报本机 IP 时要"往哪个方向出去" —— 从 URL 取主机名(域名也行, connect 会走 DNS)
_API_HOST = urlparse(SERVER_API_URL).hostname if SERVER_API_URL else None
# mt5.initialize() 不给 path 时的自动定位常失效 (报 "MetaTrader 5 x64 not found" 但其实已装),
# setup.ps1 探测到终端后会自动写入这个变量
MT5_PATH = os.getenv("MT5_PATH", "").strip()
RUNNER_STATUS_FILE = Path(__file__).resolve().parents[1] / "runner_status.json"
SELFTEST_FILE = Path(__file__).resolve().parents[1] / "selftest_result.json"  # 开机自检结果
# 终端启动配置: 固化"算法交易"等终端级开关 (克隆/重装的新机免手工点按钮)。
# 只在 bridge 拉起终端时生效; 手动双击打开的终端用它自己保存的设置。
TERMINAL_START_INI = Path(__file__).resolve().parent / "terminal_start.ini"
RESTART_FLAG = Path(__file__).resolve().parents[1] / "restart.flag"  # /restart 写它, 看门狗读它
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
UP_SINCE = datetime.now(timezone.utc).isoformat()  # 本次上线(进程启动)时刻 —
# 随心跳上报, Workers 详情显示"本次上线": update/重启后有没有真的换血一眼可查

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
_account_cache: Optional[dict] = None  # 最近一次成功读到的账户信息, 供锁被长占时应答
_fail_streak = 0  # 连续连接失败次数, 满 6 次触发自愈(杀终端重拉)
_connect_reason = ""  # 最近一次连接失败的人话原因; 经 /health→心跳→Workers 页外露


def _env_creds() -> Optional[dict]:
    """env 里的 MT5 账号三件套 → initialize 参数; 没配 = None。
    【只作兜底】: 只在"终端里确实没登录账号"时才拿出来用 —— 见 _connect() 的顺序。"""
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
    """连接 MT5, 顺序是关键(2026-08-14 与 Frank 定):

      1) 不带凭据先 initialize —— 只附着
      2) account_info() 有值 → 完事, 一个字都不碰(终端已登录的会话绝不打断)
      3) 没登录 → 才拿 env 三件套主动登录(新机/终端 profile 丢了的自愈途径)
      4) 两条都不行 → 记下原因(经 /health→心跳→Workers 页, 本地和 Linux 两边都能看到)

    为什么 2 必须在 3 前面: 原来的写法是"env 有凭据就一定带上", 于是填错一个数字
    就会把终端里正常的会话踹掉 —— 凭据从"总是优先"改成"兜底", 才不可能盖住好会话。
    """
    global _connected, _fail_streak, _connect_reason
    if MT5_PATH and not _terminal_running():
        # 终端不在时用 Popen 显式拉起(等价于用户双击, 实测这样起的终端能连),
        # 不交给 initialize 隐式拉起(实测隐式拉起的终端 IPC 附着不上);
        # 拉起后等进程出现 + 冷启动缓冲, 再握手
        logger.info("MT5 terminal not running, launching %s", MT5_PATH)
        args = [MT5_PATH]
        if TERMINAL_START_INI.exists():  # 自动开启算法交易等开关
            args.append(f"/config:{TERMINAL_START_INI}")
        try:
            subprocess.Popen(args, cwd=str(Path(MT5_PATH).parent))
        except OSError as e:
            logger.error("launch terminal failed: %s", e)
            _connect_reason = f"终端拉起失败: {e}"
            return False
        deadline = time.time() + 60
        while time.time() < deadline and not _terminal_running():
            time.sleep(3)
        time.sleep(15)

    base = {"timeout": 15_000}   # initialize 挂起期间持有 GIL, 整个进程(含 /health)冻结:
    if MT5_PATH:                 # 默认 60s 太长, 15s 快败交给重连循环再试
        base["path"] = MT5_PATH

    with _mt5_lock:
        # —— 1) 先附着(不带凭据)
        mt5.shutdown()
        ok = mt5.initialize(**base)
        info = mt5.account_info() if ok else None

        # —— 2) 已登录: 什么都不做
        if info is not None:
            _fail_streak, _connected, _connect_reason = 0, True, ""
            logger.info("MT5 connected(附着已登录会话): login=%s server=%s balance=%s",
                        info.login, info.server, info.balance)
            return True

        # —— 3) 没登录: 才用 env 凭据主动登录
        creds = _env_creds()
        if ok and creds:
            logger.info("终端未登录, 用 env 凭据登录 login=%s server=%s",
                        creds["login"], creds["server"])
            mt5.shutdown()
            ok = mt5.initialize(**base, **creds)
            info = mt5.account_info() if ok else None
            if info is not None:
                _fail_streak, _connected, _connect_reason = 0, True, ""
                logger.info("MT5 connected(env 凭据登录): login=%s server=%s balance=%s",
                            info.login, info.server, info.balance)
                return True

        # —— 4) 都不行: 记原因, 两边都看得到
        _connected = False
        if ok:
            _fail_streak = 0   # IPC 通着, 终端是好的 — 别触发杀终端自愈
            _connect_reason = ("未登录, 请检查: 终端里没有账号, env 也没配 MT5_LOGIN 三件套 —"
                               " 上机在 MT5 界面登录一次, 或在 env 填三件套让它自动登录"
                               if not creds else
                               f"未登录, 请检查: env 凭据登录失败(login={creds['login']}"
                               f" server={creds['server']}) — 账号/密码/服务器名是否正确")
            logger.error("MT5 %s", _connect_reason)
        else:
            _fail_streak += 1
            err = mt5.last_error()
            procs = _terminal_count()
            _connect_reason = f"IPC 未通: {_explain_failure(err, procs, _fail_streak)}"
            logger.error("MT5 connect failed %s | %s | %s",
                         err, _explain_failure(err, procs, _fail_streak), _diag(procs))
    return _connected


def _guarded_connect() -> bool:
    """给 _connect 包一层异常兜底(2026-08-14): 它跑在 daemon 线程里, 任何未捕获异常
    都会让线程静默死透 —— MT5 永远连不上、runner 永远卡在 wait_bridge、日志一个字都没有。
    Frank 实测踩过: env 里 MT5_LOGIN 被 dotenv 解析成注释文本, int() 抛 ValueError,
    排查了半小时。一处兜底覆盖所有失败, 比给单个 int() 打补丁更省。"""
    global _connected, _connect_reason
    try:
        return _connect()
    except Exception as e:
        _connected = False
        _connect_reason = f"连接过程异常: {type(e).__name__}: {e}"
        logger.exception("MT5 connect 未捕获异常 — 已记入状态, 交给重连循环重试")
        return False


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
        _guarded_connect()


# 本机职能(announce 应答回告): download 决定要不要轮询领下载任务; bridge 不自作主张
_role = {"download": False}

# worker 参数(config 表 worker_params, announce 应答下发): 上报节奏/批量等, 用户按网络自调。
# bridge 领回 → 内存生效 + 落文件(runner 共读决策日志保留天数等)。缺省=代码兜底值。
WORKER_PARAMS_FILE = Path(__file__).resolve().parents[1] / "worker_params.json"
_worker_params: dict = {}
try:   # 启动先读上次领的(api 没起来也用旧参数跑, 无状态: 文件只是缓存, 真相在 config 表)
    _worker_params = json.loads(WORKER_PARAMS_FILE.read_text())
except (OSError, ValueError):
    pass


def _wparam(key: str, default: int, lo: int, hi: int) -> int:
    """取 worker 参数并夹在安全区间(api 侧也校验, 双保险防脚枪)"""
    v = _worker_params.get(key, default)
    return max(lo, min(hi, v)) if isinstance(v, int) else default


def _apply_worker_params(params) -> None:
    if not isinstance(params, dict) or params == _worker_params:
        return
    _worker_params.clear()
    _worker_params.update(params)
    logger.info("worker params updated: %s", params)
    try:
        WORKER_PARAMS_FILE.write_text(json.dumps(params))
    except OSError:
        pass


def _verify_symbol(name: str):
    """品种校验(v7.2 单向化 #7, 替代 api 反向调 /symbol): 查本机 MT5。
    返回 info dict / {"error": 原因}(确定性失败) / None(MT5 没连上等瞬态, 下轮重试不缓存)"""
    try:
        with _mt5_lock:
            if mt5.terminal_info() is None:
                return None                      # 瞬态: 不给结论, 等连上再查
            found = mt5.symbol_select(name, True)
            info = mt5.symbol_info(name) if found else None
            acct = mt5.account_info()
        if info is None:
            return {"error": f"券商没有品种 {name} — 名称可能不同(如 {name}.m / Bitcoin), "
                             "在 MT5 报价窗 Ctrl+M 查实际名称"}
        return {"digits": info.digits, "point": info.point,
                "volume_min": info.volume_min, "stops_level": info.trade_stops_level,
                "broker": acct.server if acct else None}
    except Exception as e:
        logger.warning("verify symbol %s failed: %s", name, e)
        return None                              # 异常按瞬态处理, 下轮重试


def _announce_loop():
    """自动注册: 周期性向 api 自报家门, Workers 页面无需手动添加。
    身份 = 计算机名(socket.gethostname()): 稳定、重启/换IP/换账户都不变;
    IP 只作"当前地址"随心跳刷新。新机器以 download 角色入册, demo/live 由人在 web 上指派。
    顺带品种校验(单向化): 应答里领任务 → 查本机 MT5 → 下轮 announce 捎回;
    发送成功即清缓存 — api 若没入库, 下轮还会派同一任务, 自愈。"""
    if not _API_HOST or _API_HOST.startswith("127."):
        logger.warning("SERVER_API_URL 未配置或无效, 跳过自动注册 —— 请在 env/.dev.env 填"
                       " 如 SERVER_API_URL=http://192.168.4.130:8010 (当前值: %r)",
                       SERVER_API_URL)
        return
    api_base = SERVER_API_URL
    hostname = socket.gethostname()  # 计算机名 = worker 身份(注册主键)
    verify_results: dict = {}        # 待捎回的品种校验结果
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((_API_HOST, 1))   # UDP 假连接: 借目标反查本机出口网卡 IP
            my_ip = s.getsockname()[0]
            s.close()
            payload = {"name": hostname, "host": my_ip, "port": BRIDGE_PORT}
            if WORKER_KEY:
                payload["key"] = WORKER_KEY
            sent = list(verify_results)
            if sent:
                payload["symbol_info"] = dict(verify_results)
            r = requests.post(f"{api_base}/hosts/announce", timeout=10, json=payload)
            if r.status_code != 200:
                logger.warning("announce rejected: %s %s", r.status_code, r.text[:100])
            else:
                resp = r.json()
                _apply_worker_params(resp.get("params"))   # 报到领配置(config 唯一源)
                _role["download"] = bool(resp.get("download"))   # 职能以注册表为准
                for k in sent:                    # api 已收到; 没入库它会再派, 无需重发
                    verify_results.pop(k, None)
                for name in resp.get("verify_symbols", []):
                    res = _verify_symbol(name)
                    if res is not None:
                        logger.info("symbol verify %s: %s", name,
                                    res.get("error") or f"point={res.get('point')}")
                        verify_results[name] = res
                if resp.get("key_state") in ("invalid", "conflict"):
                    # 钥匙无效/被吊销/已绑别的机器(克隆机忘换钥匙的典型) — 大声说, 别静默
                    logger.warning("worker key %s — 去管理页检查(吊销了? 克隆机没换钥匙?)",
                                   resp["key_state"])
        except Exception as e:
            logger.warning("announce failed (api not up yet?): %s", e)
        time.sleep(_wparam("announce_seconds", 60, 30, 300))


def _heartbeat_push_loop():
    """v7.2 一期(2026-07-26): 主动推心跳(30s) — payload = /health 同一份数据 + name。
    api 收到即停对本机的反向探测(双栈过渡, 推送断了它自动回轮询, 两边都无需开关)。
    404 = announce 还没建档, 等下一分钟 announce 即可, 不算错。"""
    if not _API_HOST or _API_HOST.startswith("127."):
        return   # 没配 api 地址: announce 同样跳过, 这里安静退出
    api_base = SERVER_API_URL
    hostname = socket.gethostname()
    headers = {"X-API-Key": WORKER_KEY} if WORKER_KEY else {}
    trades_days = 0   # 成交窗口由 api 应答指定(自适应); 0=本拍不捎(首拍/无角色机)
    while True:
        try:
            payload = health()   # 与被轮询时同一份数据(同一个函数)
            payload["name"] = hostname
            payload["push_v"] = 1
            # 成交捎带(v7.2 #2): 按 api 上一拍指定的窗口收集; 收集失败本拍不带 —
            # api 见不到成交不打标, 自动回退拉取, 数据不丢(失败原因必须落日志)
            if trades_days and payload.get("login"):
                try:
                    payload["trades"] = _trades_data(trades_days)["deals"]
                except Exception as e:
                    logger.warning("heartbeat: collect trades(%dd) failed: %s: %s — "
                                   "本拍不捎成交, api 将回退拉取", trades_days, type(e).__name__, e)
            r = requests.post(f"{api_base}/hosts/heartbeat", timeout=15,
                              json=payload, headers=headers)
            if r.status_code == 200:
                resp = r.json()
                trades_days = int(resp.get("trades_days") or 0)
                if resp.get("trades_error"):   # api 收货失败: 原因如实打出来, 别静默
                    logger.warning("heartbeat: api 收成交失败(将回退拉取): %s", resp["trades_error"])
            elif r.status_code != 404:   # 404=announce未建档, 正常竞态, 等下一分钟
                logger.warning("heartbeat push rejected: HTTP %s %s", r.status_code, r.text[:120])
        except Exception as e:
            logger.warning("heartbeat push failed: %s: %s", type(e).__name__, e)
        # 上限60: 轮询侧"新鲜推送"窗口75s, 推得比它慢会推/拉来回抖(api侧同区间校验)
        time.sleep(_wparam("heartbeat_seconds", 30, 10, 60))


def _download_loop():
    """下载编排反转(v7.2 #3): 轮询领任务 → 本机 MT5 拉 K线 → 分批 POST 回 api 入库。
    只有 download 职能(announce 应答回告)且 MT5 已连时干活; 空闲每 20s 问一次;
    领到任务干完立刻再领(多品种连续消化, 多机 SKIP LOCKED 自动分摊)。"""
    if not _API_HOST or _API_HOST.startswith("127."):
        return
    api_base = SERVER_API_URL
    hostname = socket.gethostname()
    headers = {"X-API-Key": WORKER_KEY} if WORKER_KEY else {}
    while True:
        try:
            if not _role["download"]:
                time.sleep(30)
                continue
            with _mt5_lock:
                mt5_up = mt5.terminal_info() is not None
            if not mt5_up:          # MT5 没连: 不领任务(领了也拉不了, 白占租约)
                time.sleep(30)
                continue
            r = requests.get(f"{api_base}/download/task", timeout=15,
                             params={"name": hostname}, headers=headers)
            if r.status_code != 200:
                # 404=未注册(等announce) / 403=停用或无职能 / 其他=api侧异常 — 都带原因打日志
                logger.warning("download task poll: HTTP %s %s", r.status_code, r.text[:120])
                time.sleep(60)
                continue
            task = r.json().get("task")
            if not task:
                time.sleep(20)      # 无任务: 桌上没活, 一会儿再看
                continue
            _run_download_task(api_base, headers, task)
        except Exception as e:
            logger.warning("download loop error: %s: %s", type(e).__name__, e)
            time.sleep(30)


def _run_download_task(api_base: str, headers: dict, task: dict) -> None:
    """执行一个下载任务: [from, to) 按 bars_batch 切片拉 MT5 → 逐批上传。
    错误处理三分法:
      MT5 拉取失败(确定性) → 上报 error, job 记 FAILED+原因;
      上传网络失败        → 单批重试3次, 仍败=放弃(api 10分钟无上传自动收回重派);
      任务已失效(404/409) → 明确放弃, 回去重新领。"""
    job_id, symbol = task["job_id"], task["symbol"]
    tf_name = str(task.get("timeframe") or "M1").upper()   # 老 api 不带 = M1(向后兼容)
    tf = TIMEFRAMES.get(tf_name)
    tf_minutes = {"M1": 1, "M5": 5, "M15": 15, "M30": 30,
                  "H1": 60, "H4": 240, "D1": 1440}.get(tf_name)
    if tf is None or tf_minutes is None:
        logger.warning("download job #%s %s 未知周期 %s, 跳过", job_id, symbol, tf_name)
        return
    frm = datetime.fromisoformat(task["from"])
    to = datetime.fromisoformat(task["to"])
    batch = _wparam("bars_batch", 50000, 1000, 200000)
    chunk = timedelta(minutes=batch * tf_minutes)   # batch 根 × 每根分钟数 = 切片时长
    logger.info("download job #%s %s %s %s → %s (batch=%d bars)",
                job_id, symbol, tf_name, frm.strftime("%Y-%m-%d"), to.strftime("%Y-%m-%d"),
                batch)

    def post(payload: dict):
        for attempt in range(1, 4):
            try:
                pr = requests.post(f"{api_base}/download/bars", json=payload,
                                   headers=headers, timeout=120)
                if pr.status_code == 200:
                    return pr.json()
                if pr.status_code in (404, 409):   # 任务被新批清掉/怠工被收回: 放弃重领
                    logger.warning("download job #%s 已失效(HTTP %s %s), 放弃并重新领任务",
                                   job_id, pr.status_code, pr.text[:100])
                    return None
                logger.warning("upload bars job #%s: HTTP %s %s (第%d/3次)",
                               job_id, pr.status_code, pr.text[:120], attempt)
            except Exception as e:
                logger.warning("upload bars job #%s failed: %s: %s (第%d/3次)",
                               job_id, type(e).__name__, e, attempt)
            time.sleep(5)
        logger.error("download job #%s 上传连败3次, 放弃 — api 将在10分钟后收回重派", job_id)
        return None

    cursor = frm
    seen_data = False   # 本任务是否已经拉到过数据 — None 的裁决依据(头部 vs 中途)
    rest_bars = _wparam("dl_rest_bars", 1000000, 0, 5_000_000)  # 节流: 每拉N根歇一会(0=不歇)
    rest_secs = _wparam("dl_rest_secs", 30, 5, 600)
    pulled_since_rest = 0
    while cursor < to:
        chunk_end = min(cursor + chunk, to)
        data = None
        for attempt in range(3):    # None 先重试: 瞬时故障(终端忙/历史在同步)几秒就好
            with _mt5_lock:
                mt5.symbol_select(symbol, True)
                data = mt5.copy_rates_range(symbol, tf, cursor, chunk_end)
            if data is not None:
                break
            time.sleep(3)
        if data is None:
            with _mt5_lock:
                err = mt5.last_error()
            # 裁决(2026-07-28 Frank 定"可以空跑但不能乱跳开"): 还没见过数据 = 超出券商
            # 历史深度的头部, 允许跳过继续巡逻(空跑); 中途出 None = 真故障, 如实报错
            # FAILED 重派 — 重派整段幂等重下, 宁可重跑不留洞。
            if seen_data:
                post({"job_id": job_id,
                      "error": f"copy_rates_range {symbol} {cursor:%Y-%m-%d}~{chunk_end:%Y-%m-%d}"
                               f" 中途失败(重试3次): {err} — 任务将重派整段重下"})
                return
            logger.warning("download job #%s %s %s~%s 头部无数据(%s), 跳过继续巡逻",
                           job_id, symbol, cursor.strftime("%Y-%m-%d"),
                           chunk_end.strftime("%Y-%m-%d"), err)
            data = []
        if len(data):
            seen_data = True
        bars = [{"time": int(b["time"]),
                 "open": float(b["open"]), "high": float(b["high"]),
                 "low": float(b["low"]), "close": float(b["close"]),
                 "tick_volume": int(b["tick_volume"]), "spread": int(b["spread"]),
                 "real_volume": int(b["real_volume"])} for b in data]
        if post({"job_id": job_id, "bars": bars, "done": chunk_end >= to}) is None:
            return
        cursor = chunk_end
        # 节流(2026-07-29 Frank 定): 满速首灌会把 CPU/库打满且心跳饿死(bridge MT5 锁全局
        # 串行) — 每拉够 N 根在锁外歇一会, 终端降温 + 心跳插队。api 10分钟怠工阈值远大于此。
        pulled_since_rest += len(bars)
        if rest_bars and pulled_since_rest >= rest_bars and cursor < to:
            logger.info("download job #%s throttle: %d bars pulled, resting %ds",
                        job_id, pulled_since_rest, rest_secs)
            pulled_since_rest = 0
            time.sleep(rest_secs)
    logger.info("download job #%s %s done", job_id, symbol)


def _require_connected():
    if not _connected:
        raise HTTPException(status_code=503, detail="MT5 not connected")


app = FastAPI(title="MT5 Bridge", version="2.0.0")


@app.on_event("startup")
def startup():
    # _connect() 必须放后台线程: uvicorn 等 startup 跑完才开始监听端口,
    # 而 mt5.initialize() 在终端卡弹窗/首启慢时会挂起几十秒甚至更久,
    # 同步调用会导致 8020 整个起不来, /health 不可达, 心跳误判离线
    threading.Thread(target=_guarded_connect, daemon=True).start()
    threading.Thread(target=_reconnect_loop, daemon=True).start()
    threading.Thread(target=_announce_loop, daemon=True).start()
    threading.Thread(target=_heartbeat_push_loop, daemon=True).start()
    threading.Thread(target=_download_loop, daemon=True).start()


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
        # mt5_reason: 最近一次连接失败的人话原因(2026-08-14) —— 经心跳搬到 Linux 的
        # last_health, Workers 页直接显示。以前只有"未连接"三个字, 得上机翻 bridge 日志
        # 才知道是"没登录账号"还是"IPC 不通"还是"终端僵死"
        return {"status": "degraded", "mt5_connected": False, "runner": runner,
                "mt5_reason": _connect_reason,
                "selftest": _selftest(), "version": VERSION, "up_since": UP_SINCE,
                "dl_poll": True, "dl_tf": True, "services": services, "summary": summary}
    try:   # 持仓快照(v7.2 #5 单向化): 每拍随心跳覆盖到 last_health, web 流水页读它
        positions = _positions_snapshot()   # 不再反向拉 — 失败给空+日志, 不拖垮心跳
    except Exception as e:
        logger.warning("positions snapshot failed: %s: %s", type(e).__name__, e)
        positions = []
    return {
        "status": "healthy",
        "version": VERSION,
        "up_since": UP_SINCE,
        "dl_poll": True,   # 能力标记(v7.2 #3): 本机会轮询领下载任务 → api 走 jobs 模式不再反向拉
        "dl_tf": True,     # 能力标记(2026-07-29): 认识任务的 timeframe 字段 → 才配领非 M1 任务
        "mt5_connected": True,
        "trade_allowed": account["trade_allowed"],
        "login": account["login"],
        "server": account["server"],
        "currency": account["currency"],
        "positions": positions,
        "runner": runner,
        "selftest": _selftest(),
        "services": services,
        "summary": summary,
    }


@app.post("/restart")
def remote_restart():
    """远程重启服务 (不更新代码 — 更新请在 Windows 上手动 update.bat)。
    机制: 写 restart.flag + 主动退出。start_bridge.bat 看门狗在两次循环之间读到标志,
    连 runner 一起重启并重跑自检。顺着看门狗而非对抗它, 无进程互杀/抢锁。"""
    logger.info("remote restart requested")
    RESTART_FLAG.write_text("1")

    def _exit():
        time.sleep(1)   # 让 HTTP 响应先发出去
        os._exit(0)     # 硬退出 → python main.py 返回, 看门狗接管
    threading.Thread(target=_exit, daemon=True).start()
    return {"started": True, "note": "worker 将离线约1分钟, bridge/runner 重启并重跑自检"}


# /connect(远程下发账户)已删(2026-07-26 v7.2 收口): api 侧调用早已砍掉, 端点成孤儿 —
# 账户 = 部署时写机器 env / MT5 手动登录, announce/心跳自动回报实际账号。


@app.get("/account")
def account():
    _require_connected()
    with _mt5_lock:
        info = mt5.account_info()
    if info is None:
        raise HTTPException(status_code=500, detail="account_info failed")
    return info._asdict()


@app.get("/symbols")
def symbols():
    _require_connected()
    with _mt5_lock:
        result = mt5.symbols_get()
    return {"symbols": [s.name for s in (result or [])]}


@app.get("/symbol/{symbol}")
def symbol_info(symbol: str):
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
):
    """按时间范围取K线, 供 app 下载器分页拉取"""
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


def _positions_snapshot() -> list:
    """当前持仓快照(MT5 原样序列化): /trades 与 心跳payload(v7.2 #5 单向化)共用同一份字段。"""
    with _mt5_lock:
        positions = mt5.positions_get() or []
    return [{
        "ticket": p.ticket, "time": p.time, "symbol": p.symbol,
        "type": _DEAL_TYPE.get(p.type, str(p.type)), "volume": p.volume,
        "price_open": p.price_open, "sl": p.sl, "tp": p.tp,
        "price_current": p.price_current, "profit": p.profit, "swap": p.swap,
        "magic": p.magic, "comment": p.comment,
    } for p in positions]


def _trades_data(days: int) -> dict:
    now = datetime.now(timezone.utc)
    positions = _positions_snapshot()
    with _mt5_lock:  # +1天缓冲: 历史过滤按券商服务器时间
        deals = mt5.history_deals_get(now - timedelta(days=days), now + timedelta(days=1)) or []
    return {
        "days": days,
        "positions": positions,
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
def trades(days: int = 30, fmt: str = "json"):
    """交易流水(只读): 当前持仓 + 历史成交明细, 原样透传 MT5。
    json 给 api/web /mt5 页用; fmt=html 本机浏览器直接看(与状态页同级)。
    时间是 epoch 秒(券商服务器时钟); deals 按时间倒序。"""
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
        # 成交模式自适应(与 runner 同修, 2026-07-26 事故 retcode 10030): 品种支持什么用什么。
        # 位掩码字面值(MQL5 定值 FOK=1/IOC=2): Python 包没有 SYMBOL_FILLING_* 常量
        fm = (mt5.ORDER_FILLING_IOC if info.filling_mode & 2
              else mt5.ORDER_FILLING_FOK if info.filling_mode & 1
              else mt5.ORDER_FILLING_RETURN)
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
               "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
               "sl": tick.ask - dist, "tp": tick.ask + dist, "deviation": 20,
               "magic": SMOKE_MAGIC, "comment": "bridge-ordertest",
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": fm}
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
                            "type_filling": fm})
    if c is None or c.retcode != mt5.TRADE_RETCODE_DONE:
        return {"result": "PARTIAL", "open": opened,
                "close": f"failed retcode={c.retcode if c else None} - close manually in MT5"}
    logger.info("ordertest PASS: open %.5f close %.5f", opened["price"], c.price)
    return {"result": "PASS", "open": opened, "close": {"price": c.price},
            "note": "full order path works; cost = one spread"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)
