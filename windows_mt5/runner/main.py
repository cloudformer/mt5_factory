"""实时执行 Runner - 与 MT5 同机运行, 加载 DEMO/LIVE 策略实盘执行

纪律(CLAUDE.md):
- 只在已收盘 bar 上决策 (copy_rates_from_pos 从位置1取, 跳过未走完的 bar)
- 每笔订单必带服务端 SL/TP
- 无状态: 持仓真相永远来自 MT5 (positions_get by magic), 不信任内存
- 每策略独立 magic number + 异常隔离(单策略报错不影响其他)
"""
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# strategy_core 在 repo 根目录; conn 包在 windows_mt5 (整仓 clone 到 Windows)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5
import requests
from dotenv import load_dotenv

from conn import stats
from strategy_core import make_strategy

# 统一配置: 与 Linux docker compose 共用 env/.dev.env (整仓 clone 到 Windows)
load_dotenv(Path(__file__).resolve().parents[2] / "env" / ".dev.env")

# api 地址由共享配置拼出: http://<Linux VM IP>:<API_PORT>
DOCKER_COMPOSE_HOST = os.getenv("DOCKER_COMPOSE_HOST", "").strip()
API_URL = f"http://{DOCKER_COMPOSE_HOST}:{os.getenv('API_PORT', '8010')}"
RUN_STATUS = os.getenv("RUN_STATUS", "DEMO")
VOLUME = float(os.getenv("VOLUME", "0.01"))
# mt5.initialize() 不给 path 时的自动定位常失效 (报 "MetaTrader 5 x64 not found" 但其实已装),
# setup.ps1 探测到终端后会自动写入这个变量
MT5_PATH = os.getenv("MT5_PATH", "").strip()
BRIDGE_PORT = int(os.getenv("MT5_PORT", "8020"))  # 同机 bridge, 开机时等它先连上 MT5
STATUS_FILE = Path(__file__).resolve().parents[1] / "runner_status.json"  # bridge 状态页读它
POLL_SECONDS = 10       # 主循环: 写心跳 + 处理收盘bar
REFRESH_SECONDS = 15    # 重新检测角色/拉策略 (原60s太久, 切换后要等太长; 收到15s 更快反映)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
)
logger = logging.getLogger("runner")

TF_MT5 = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def write_status(run_status: str, instances: list, last_bar: dict | None = None,
                 skipped: list | None = None) -> None:
    """心跳落盘, 供 bridge 状态页/上报使用。附带账户快照 + 每策略战绩 (conn/stats 采集);
    统计失败不影响交易主循环, 缺失字段前端显示为"—"。"""
    payload = {
        "updated": time.time(),
        "run_status": run_status,
        "strategies": len(instances),
        "mt5_connected": mt5.terminal_info() is not None,
    }
    payload["skipped"] = skipped or []
    try:
        payload["account"] = stats.account_snapshot()
        payload["per_strategy"] = stats.per_strategy(instances, last_bar)
    except Exception as e:
        logger.warning("stats collect failed: %s", e)
    try:
        STATUS_FILE.write_text(json.dumps(payload))
    except OSError:
        pass


def wait_bridge() -> None:
    """等同机 bridge 连上 MT5 后才附着, 等多久都等, 绝不自行 initialize:
    终端的拉起/连接/自愈全部由 bridge 一家负责, runner 多一个进程去碰终端
    只会制造并发握手干扰(实测两进程同时 initialize 双双 IPC timeout)。
    bridge 没连上期间 runner 本来就无事可做(不该交易), 等待就是正确行为。"""
    logger.info("runner is up (NOT stuck), waiting for local bridge to connect MT5 before attaching...")
    waited = 0
    while True:
        try:
            h = requests.get(f"http://127.0.0.1:{BRIDGE_PORT}/health", timeout=5).json()
            if h.get("mt5_connected"):
                logger.info("bridge reports MT5 connected, runner attaching now")
                return
            why = "bridge is up but MT5 not connected yet (bridge owns connect/self-heal, see bridge window)"
        except (requests.RequestException, ValueError):
            why = "bridge not responding (starting/restarting?)"
        write_status("等待 MT5", [], skipped=[])
        time.sleep(10)
        waited += 10
        if waited % 60 == 0:
            logger.info("still waiting: %s (%ds elapsed)", why, waited)


def mt5_connect() -> bool:
    login = os.getenv("MT5_LOGIN", "").strip()
    kwargs = {"path": MT5_PATH} if MT5_PATH else {}
    if login:
        kwargs.update(login=int(login), password=os.getenv("MT5_PASSWORD", ""),
                      server=os.getenv("MT5_SERVER", ""))
    # 附着挂起(如终端未就绪)会冻结整个进程, 快败快重试
    kwargs["timeout"] = 15_000
    return mt5.initialize(**kwargs)  # 无账户时附着到已登录终端


def detect_run_status() -> str:
    """本机职能以 web 上的指派为准 (mt5_hosts.runner): live→LIVE, demo→DEMO, NULL→不跑;
    按计算机名(gethostname)匹配自己那行 — 与 bridge 注册的身份一致, 不受 IP 变化影响。
    找不到本机注册记录时退回 env 的 RUN_STATUS"""
    try:
        hostname = socket.gethostname()
        r = requests.get(f"{API_URL}/hosts", timeout=10)
        for h in r.json()["hosts"]:
            if h["name"] == hostname and h["enabled"]:
                return {"live": "LIVE", "demo": "DEMO"}.get(h["runner"], "")
    except Exception as e:
        logger.warning("role detect failed (%s), fallback to env RUN_STATUS", e)
    return RUN_STATUS


_vol_seen: dict = {}   # 上一轮各策略手数(仅日志比对用, 丢了无害 — 状态仍只在DB)


def fetch_strategies(run_status: str) -> list:
    """从 api 拉取本 worker 应运行的策略实例"""
    if not run_status:
        logger.info("loaded 0 strategies (role=idle)")  # 空闲也打一行, 否则控制台静默像卡住
        return [], []   # 空闲: 无角色 → 不加载任何策略 (必须返回两个值, 与解包对齐)
    r = requests.get(f"{API_URL}/strategies/status",
                     params={"status": run_status, "limit": 500}, timeout=10)
    r.raise_for_status()
    data = r.json()
    # 默认手数唯一源=config表(接口随策略带回); env VOLUME 只在 api 没给时兜底
    default_vol = float(data.get("volume_default") or VOLUME)
    instances, skipped = [], []
    for s in data["strategies"]:
        try:
            if s["timeframe"] not in TF_MT5:  # 脏 timeframe: 加载时就跳过, 不进主循环每轮 KeyError 刷屏
                logger.warning("unsupported timeframe %s, skip %s", s["timeframe"], s["name"])
                skipped.append({"id": s["id"], "name": s["name"], "symbol": s["symbol"],
                                "reason": f"bad_timeframe:{s['timeframe']}"})
                continue
            params = s["params"] if isinstance(s["params"], dict) else json.loads(s["params"])
            mt5.symbol_select(s["symbol"], True)
            info = mt5.symbol_info(s["symbol"])
            if info is None:
                # 走到这已确认连接在(主循环断线会先重连), 故这里是真·单品种不可用: 记 last_error 便于分辨名字问题
                logger.warning("symbol %s unavailable (err=%s), skip %s",
                               s["symbol"], mt5.last_error(), s["name"])
                skipped.append({"id": s["id"], "name": s["name"], "symbol": s["symbol"],
                                "reason": "not_in_market_watch"})
                continue
            instances.append({
                "id": s["id"], "name": s["name"], "symbol": s["symbol"],
                "timeframe": s["timeframe"], "magic": s["magic_number"] or 100000 + s["id"],
                # 每策略手数(strategies.volume): 空=用默认(config.volume_default)。
                # 每轮拉取即刷新 → web 改手数下一单生效, 不用重启(runner 无状态)
                "volume": float(s.get("volume") or default_vol),
                "strategy": make_strategy(s["template"], params, info.point),
            })
        except Exception as e:
            logger.error("build strategy %s failed: %s", s.get("name"), e)
    # 手数可观测: 与上一轮比对, 变了打一行(web 改手数 → 这里确认已生效); 自定义手数随加载汇总
    for inst in instances:
        prev = _vol_seen.get(inst["id"])
        if prev is not None and abs(prev - inst["volume"]) > 1e-9:
            logger.info("volume change #%d %s: %.2f -> %.2f (next order)",
                        inst["id"], inst["name"], prev, inst["volume"])
        _vol_seen[inst["id"]] = inst["volume"]
    customs = [i for i in instances if abs(i["volume"] - default_vol) > 1e-9]
    logger.info("loaded %d strategies, skipped %d (role=%s)%s",
                len(instances), len(skipped), run_status or "idle",
                " | custom volume: " + ", ".join(
                    f"#{i['id']}={i['volume']:g}" for i in customs) if customs else "")
    return instances, skipped


def has_position(symbol: str, magic: int) -> bool:
    positions = mt5.positions_get(symbol=symbol)
    return any(p.magic == magic for p in (positions or []))


def send_order(inst: dict, sig) -> None:
    if not sig.sl or not sig.tp:  # 铁律: 无 SL/TP 不下单
        logger.error("%s signal without SL/TP, refused", inst["name"])
        return
    info = mt5.symbol_info(inst["symbol"])  # 下单前取现值: digits/最小手/停损距离都可能随券商变
    tick = mt5.symbol_info_tick(inst["symbol"])
    if info is None or tick is None:
        logger.error("%s no symbol info / tick", inst["symbol"])
        return
    is_buy = sig.direction == "BUY"
    price = tick.ask if is_buy else tick.bid
    # A: SL/TP 按券商 digits 取整 — 浮点原值(如 4083.8400000001)苛刻券商会拒 Invalid stops
    sl, tp = round(sig.sl, info.digits), round(sig.tp, info.digits)
    # B1: 手数不得低于品种最小手 (拒单前置成明确日志, 不留给券商猜)
    volume = inst.get("volume") or VOLUME   # 每策略手数, 空=env 默认(兜底)
    if info.volume_min and volume < info.volume_min:
        logger.error("%s volume %.2f < broker min %.2f, refused",
                     inst["name"], volume, info.volume_min)
        return
    # B2: SL/TP 距离不得小于券商最小停损距离 stops_level (小止损策略必须过这关)
    min_d = (info.trade_stops_level or 0) * info.point
    if min_d and (abs(price - sl) < min_d or abs(tp - price) < min_d):
        logger.error("%s SL/TP too close to price: |price-sl|=%.5f |tp-price|=%.5f < stops_level %.5f, refused",
                     inst["name"], abs(price - sl), abs(tp - price), min_d)
        return
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": inst["symbol"],
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": inst["magic"],
        # 下单备注(跟单进券商, 任何MT5终端可见): id:744,win-worker01 — 策略id定位+哪台机器下的。
        # MT5 comment上限约31字符, 截到26留券商后缀余量; id在前保证永远完整, 超长只丢机器名尾部。
        # 方向/品种/时间不写 — MT5 原生字段已有, 不重复占位。权威归因仍是 magic。
        "comment": f"id:{inst['id']},{socket.gethostname()}"[:26],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("%s order failed: %s", inst["name"],
                     result._asdict() if result else mt5.last_error())
    else:
        logger.info("%s %s %.2f lots @ %.5f sl=%.5f tp=%.5f (magic=%d)",
                    inst["name"], sig.direction, volume, result.price,
                    sig.sl, sig.tp, inst["magic"])


def process(inst: dict, last_bar: dict) -> None:
    strat = inst["strategy"]
    tf = TF_MT5[inst["timeframe"]]
    # 从位置1取 = 只用已收盘 bar
    rates = mt5.copy_rates_from_pos(inst["symbol"], tf, 1, strat.warmup)
    if rates is None or len(rates) < strat.warmup:
        return
    bar_time = int(rates[-1]["time"])
    if last_bar.get(inst["id"]) == bar_time:  # 该收盘bar已处理过
        return
    last_bar[inst["id"]] = bar_time

    sig = strat.on_bar(rates["open"], rates["high"], rates["low"], rates["close"])
    if sig is None:
        return
    if has_position(inst["symbol"], inst["magic"]):  # 无状态: 持仓以MT5为准
        return
    send_order(inst, sig)


def main():
    if not DOCKER_COMPOSE_HOST or DOCKER_COMPOSE_HOST.startswith("127."):
        logger.error("DOCKER_COMPOSE_HOST in env/.dev.env must be the Linux VM LAN IP (current: %r)", DOCKER_COMPOSE_HOST)
        sys.exit(1)
    while True:
        wait_bridge()  # 返回即 bridge 已确认 MT5 可用
        if mt5_connect():
            break
        logger.error("MT5 attach failed %s | bridge side is fine, treating as transient, back to waiting",
                     mt5.last_error())
        time.sleep(10)
    # 启动横幅, 只打一次。进程起来了; 角色(idle/demo/live)看每轮的 "role=" 行, 不在这里断言
    logger.info("runner process OK (volume=%s)", VOLUME)

    instances, skipped, last_bar, last_refresh, run_status = [], [], {}, 0.0, ""
    while True:
        # 连接自愈: 附着后 MT5 可能掉线(终端更新/重启, 或 bridge 自愈重连使旧 handle 失效),
        # MetaTrader5 掉线不自愈 → 必须重新附着。否则 symbol_info 恒为 None,
        # 所有策略被误判"无报价"卡死, 只能靠看门狗整进程重启才恢复(实测过)。
        if mt5.terminal_info() is None:
            logger.warning("MT5 connection lost, re-attaching (else everything falsely skipped as 'no quote')")
            mt5.shutdown()
            wait_bridge()                 # 等同机 bridge 把终端重新连好, 再附着
            if not mt5_connect():
                logger.error("re-attach failed: %s | retry in 10s", mt5.last_error())
                write_status("重连中", [], skipped=[])
                time.sleep(10)
                continue
            logger.info("MT5 re-attached OK")
            last_refresh = 0.0            # 立刻重拉策略(重跑 symbol_select)
        if time.time() - last_refresh > REFRESH_SECONDS:
            try:
                run_status = detect_run_status()
                instances, skipped = fetch_strategies(run_status)
                last_refresh = time.time()
            except Exception as e:
                logger.error("fetch strategies failed: %s", e)
        for inst in instances:
            try:
                process(inst, last_bar)
            except Exception as e:  # 异常隔离: 单策略失败不拖累其他
                logger.error("strategy %s error: %s", inst["name"], e)
        write_status(run_status or "未指派", instances, last_bar, skipped)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
