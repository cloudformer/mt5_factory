"""实时执行 Runner (attach-only rewrite) - 与 MT5 同机, 加载 DEMO/LIVE 策略执行。

关键变化 (相比旧版):
- 连接委托给 mt5_conn (与 bridge 共用的冻结核心), 不再自己 initialize/等 bridge。
- 没有角色时 (download-only / 未指派) 彻底不碰 MT5 —— 数据下载阶段只让 bridge 一个
  进程连终端, 避免两进程抢 IPC。只有被指派 demo/live 时才附着并交易。

纪律(CLAUDE.md): 只在收盘 bar 决策; 每单必带 SL/TP; 无状态(持仓真相来自 MT5);
每策略独立 magic + 异常隔离。
"""
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

# conn 包在 windows_mt5 (parents[1]); 先加它, 再用 conn.paths 定位 repo 根 (无魔法数字)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import MetaTrader5 as mt5
import requests
from dotenv import load_dotenv

from conn import mt5_conn, stats
from conn.paths import ENV_FILE, REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))   # strategy_core 在 repo 根
from strategy_core import make_strategy  # noqa: E402

load_dotenv(ENV_FILE)

DOCKER_COMPOSE_HOST = os.getenv("DOCKER_COMPOSE_HOST", "").strip()
API_URL = f"http://{DOCKER_COMPOSE_HOST}:{os.getenv('API_PORT', '8010')}"
VOLUME = float(os.getenv("VOLUME", "0.01"))
STATUS_FILE = Path(__file__).resolve().parents[1] / "runner_status.json"
POLL_SECONDS = 10
REFRESH_SECONDS = 60

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
)
logger = logging.getLogger("runner")

TF_MT5 = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
          "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
          "D1": mt5.TIMEFRAME_D1}


def _creds():
    login = os.getenv("MT5_LOGIN", "").strip()
    if not login:
        return None
    return {"login": int(login), "password": os.getenv("MT5_PASSWORD", ""),
            "server": os.getenv("MT5_SERVER", "")}


def write_status(run_status: str, instances: list, last_bar: dict | None = None,
                 skipped: list | None = None) -> None:
    """心跳落盘, 供 bridge 状态页/上报使用。附带账户快照 + 每策略战绩 (conn/stats 采集);
    统计失败不影响交易主循环, 缺失字段前端显示为"—"。"""
    payload = {
        "updated": time.time(),
        "run_status": run_status,
        "strategies": len(instances),
        "mt5_connected": mt5_conn.is_connected(),
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


def detect_run_status() -> str:
    """本机职能以 web 指派为准 (mt5_hosts.runner): live->LIVE, demo->DEMO, NULL->不跑"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((DOCKER_COMPOSE_HOST, 1))
        my_ip = s.getsockname()[0]
        s.close()
        r = requests.get(f"{API_URL}/hosts", timeout=10)
        for h in r.json()["hosts"]:
            if h["host"] == my_ip and h["enabled"]:
                return {"live": "LIVE", "demo": "DEMO"}.get(h["runner"], "")
    except Exception as e:
        logger.warning("role detect failed: %s", e)
    return ""


def fetch_strategies(run_status: str) -> list:
    if not run_status:
        return []
    r = requests.get(f"{API_URL}/strategies/status",
                     params={"status": run_status, "limit": 500}, timeout=10)
    r.raise_for_status()
    instances, skipped = [], []
    for s in r.json()["strategies"]:
        try:
            params = s["params"] if isinstance(s["params"], dict) else json.loads(s["params"])
            with mt5_conn.lock():
                mt5.symbol_select(s["symbol"], True)
                info = mt5.symbol_info(s["symbol"])
            if info is None:
                logger.warning("symbol %s unavailable, skip %s", s["symbol"], s["name"])
                skipped.append({"id": s["id"], "name": s["name"], "symbol": s["symbol"],
                                "reason": "not_in_market_watch"})
                continue
            instances.append({"id": s["id"], "name": s["name"], "symbol": s["symbol"],
                              "timeframe": s["timeframe"],
                              "magic": s["magic_number"] or 100000 + s["id"],
                              "strategy": make_strategy(s["template"], params, info.point)})
        except Exception as e:
            logger.error("build strategy %s failed: %s", s.get("name"), e)
    logger.info("loaded %d strategies, skipped %d (status=%s)",
                len(instances), len(skipped), run_status)
    return instances, skipped


def has_position(symbol: str, magic: int) -> bool:
    with mt5_conn.lock():
        positions = mt5.positions_get(symbol=symbol)
    return any(p.magic == magic for p in (positions or []))


def send_order(inst: dict, sig) -> None:
    if not sig.sl or not sig.tp:  # 铁律: 无 SL/TP 不下单
        logger.error("%s signal without SL/TP, refused", inst["name"])
        return
    with mt5_conn.lock():
        tick = mt5.symbol_info_tick(inst["symbol"])
        if tick is None:
            logger.error("%s no tick", inst["symbol"])
            return
        is_buy = sig.direction == "BUY"
        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": inst["symbol"], "volume": VOLUME,
                   "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                   "price": tick.ask if is_buy else tick.bid, "sl": sig.sl, "tp": sig.tp,
                   "deviation": 20, "magic": inst["magic"], "comment": inst["name"][:26],
                   "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
        result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("%s order failed: %s", inst["name"],
                     result._asdict() if result else mt5.last_error())
    else:
        logger.info("%s %s %.2f lots @ %.5f sl=%.5f tp=%.5f (magic=%d)", inst["name"],
                    sig.direction, VOLUME, result.price, sig.sl, sig.tp, inst["magic"])


def process(inst: dict, last_bar: dict) -> None:
    strat = inst["strategy"]
    tf = TF_MT5[inst["timeframe"]]
    with mt5_conn.lock():
        rates = mt5.copy_rates_from_pos(inst["symbol"], tf, 1, strat.warmup)  # 位置1=只用收盘bar
    if rates is None or len(rates) < strat.warmup:
        return
    bar_time = int(rates[-1]["time"])
    if last_bar.get(inst["id"]) == bar_time:
        return
    last_bar[inst["id"]] = bar_time
    sig = strat.on_bar(rates["open"], rates["high"], rates["low"], rates["close"])
    if sig is None:
        return
    if has_position(inst["symbol"], inst["magic"]):  # 无状态: 持仓以 MT5 为准
        return
    send_order(inst, sig)


def main():
    if not DOCKER_COMPOSE_HOST or DOCKER_COMPOSE_HOST.startswith("127."):
        logger.error("DOCKER_COMPOSE_HOST in env/.dev.env must be the Linux VM LAN IP (got %r)",
                     DOCKER_COMPOSE_HOST)
        sys.exit(1)
    logger.info("runner up; stays OUT of MT5 until this host is assigned demo/live on the web")

    instances, skipped, last_bar, last_refresh, run_status, attached = [], [], {}, 0.0, "", False
    while True:
        if time.time() - last_refresh > REFRESH_SECONDS:
            try:
                run_status = detect_run_status()
                last_refresh = time.time()
            except Exception as e:
                logger.error("refresh role failed: %s", e)

        if not run_status:
            # 无角色: 绝不碰 MT5 (数据下载阶段让 bridge 独占终端)
            if attached:
                mt5_conn.drop()
                attached = False
            instances = []
            write_status("未指派", [], skipped=[])
            time.sleep(POLL_SECONDS)
            continue

        if not attached:
            # 被指派了才附着; 自己的连接用于 order_send
            if mt5_conn.attach(_creds()):
                attached = True
                instances, skipped = fetch_strategies(run_status)
                logger.info("attached for %s trading", run_status)
            else:
                logger.warning("attach failed (terminal open & logged in?): %s", mt5_conn.last_error())
                write_status("连接中", [], skipped=[])
                time.sleep(10)
                continue

        if time.time() - last_refresh < POLL_SECONDS:  # 刚刷新过角色, 顺带刷策略
            try:
                instances, skipped = fetch_strategies(run_status)
            except Exception as e:
                logger.error("fetch strategies failed: %s", e)

        for inst in instances:
            try:
                process(inst, last_bar)
            except Exception as e:  # 异常隔离
                logger.error("strategy %s error: %s", inst["name"], e)
        write_status(run_status, instances, last_bar, skipped)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
