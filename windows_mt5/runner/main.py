"""实时执行 Runner - 与 MT5 同机运行, 加载 DEMO/ACTIVE 策略实盘执行

纪律(CLAUDE.md):
- 只在已收盘 bar 上决策 (copy_rates_from_pos 从位置1取, 跳过未走完的 bar)
- 每笔订单必带服务端 SL/TP
- 无状态: 持仓真相永远来自 MT5 (positions_get by magic), 不信任内存
- 每策略独立 magic number + 异常隔离(单策略报错不影响其他)
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

# strategy_core 在 repo 根目录 (整仓 clone 到 Windows)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import MetaTrader5 as mt5
import requests
from dotenv import load_dotenv

from strategy_core import make_strategy

load_dotenv(Path(__file__).resolve().parents[1] / "worker.env")

APP_URL = os.getenv("APP_URL", "").rstrip("/")
RUN_STATUS = os.getenv("RUN_STATUS", "DEMO")
VOLUME = float(os.getenv("VOLUME", "0.01"))
POLL_SECONDS = 10
REFRESH_SECONDS = 60

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


def mt5_connect() -> bool:
    login = os.getenv("MT5_LOGIN", "").strip()
    if login:
        return mt5.initialize(login=int(login), password=os.getenv("MT5_PASSWORD", ""),
                              server=os.getenv("MT5_SERVER", ""))
    return mt5.initialize()  # 附着到已登录终端


def fetch_strategies() -> list:
    """从 app 拉取本 worker 应运行的策略实例"""
    r = requests.get(f"{APP_URL}/strategies/status",
                     params={"status": RUN_STATUS, "limit": 500}, timeout=10)
    r.raise_for_status()
    instances = []
    for s in r.json()["strategies"]:
        try:
            params = s["params"] if isinstance(s["params"], dict) else json.loads(s["params"])
            mt5.symbol_select(s["symbol"], True)
            info = mt5.symbol_info(s["symbol"])
            if info is None:
                logger.warning("symbol %s unavailable, skip %s", s["symbol"], s["name"])
                continue
            instances.append({
                "id": s["id"], "name": s["name"], "symbol": s["symbol"],
                "timeframe": s["timeframe"], "magic": s["magic_number"] or 100000 + s["id"],
                "strategy": make_strategy(s["template"], params, info.point),
            })
        except Exception as e:
            logger.error("build strategy %s failed: %s", s.get("name"), e)
    logger.info("loaded %d strategies (status=%s)", len(instances), RUN_STATUS)
    return instances


def has_position(symbol: str, magic: int) -> bool:
    positions = mt5.positions_get(symbol=symbol)
    return any(p.magic == magic for p in (positions or []))


def send_order(inst: dict, sig) -> None:
    if not sig.sl or not sig.tp:  # 铁律: 无 SL/TP 不下单
        logger.error("%s signal without SL/TP, refused", inst["name"])
        return
    tick = mt5.symbol_info_tick(inst["symbol"])
    if tick is None:
        logger.error("%s no tick", inst["symbol"])
        return
    is_buy = sig.direction == "BUY"
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": inst["symbol"],
        "volume": VOLUME,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if is_buy else tick.bid,
        "sl": sig.sl,
        "tp": sig.tp,
        "deviation": 20,
        "magic": inst["magic"],
        "comment": inst["name"][:26],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("%s order failed: %s", inst["name"],
                     result._asdict() if result else mt5.last_error())
    else:
        logger.info("%s %s %.2f lots @ %.5f sl=%.5f tp=%.5f (magic=%d)",
                    inst["name"], sig.direction, VOLUME, result.price,
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
    if not APP_URL:
        logger.error("APP_URL not set in worker.env")
        sys.exit(1)
    while not mt5_connect():
        logger.error("MT5 connect failed: %s, retry in 30s", mt5.last_error())
        time.sleep(30)
    logger.info("runner started (status=%s, volume=%s)", RUN_STATUS, VOLUME)

    instances, last_bar, last_refresh = [], {}, 0.0
    while True:
        if time.time() - last_refresh > REFRESH_SECONDS:
            try:
                instances = fetch_strategies()
                last_refresh = time.time()
            except Exception as e:
                logger.error("fetch strategies failed: %s", e)
        for inst in instances:
            try:
                process(inst, last_bar)
            except Exception as e:  # 异常隔离: 单策略失败不拖累其他
                logger.error("strategy %s error: %s", inst["name"], e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
