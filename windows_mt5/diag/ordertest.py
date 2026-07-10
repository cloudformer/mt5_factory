"""Order-path smoke test - fires ONE minimal real order then closes it immediately.

Answers one question: can this machine actually SEND ORDERS through the exact
request shape runner uses? (AutoTrading on, volume/filling-mode/stops accepted)
Strategy logic is proven separately by the web page showing advancing bar times;
this covers the only remaining unverified link: broker-side order_send.

SAFETY: refuses to run on a REAL account - demo only.
Run via ordertest.bat. Prereq: MT5 terminal open + logged in to demo.
English-only output: the Windows console codepage (GBK) garbles Chinese.
"""
import sys

import MetaTrader5 as mt5

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
MAGIC = 999999  # never used by real strategies (theirs are 100000 + id)

# retcode -> what it means and what to do (the common failure modes)
HINTS = {
    10027: "AutoTrading is DISABLED in the terminal -> click the 'Algo Trading' toolbar button ON, re-run",
    10026: "Auto trading disabled by BROKER for this account -> use another demo account",
    10030: "Broker does not support IOC filling -> tell Claude: change type_filling in runner send_order",
    10014: "Invalid volume -> broker minimum differs, see volume_min above",
    10016: "Invalid stops -> SL/TP too close, see trade_stops_level above",
    10018: "Market closed -> re-run during market hours",
    10019: "Not enough money on the demo account -> top up / new demo account",
}


def die(msg: str) -> None:
    print("\n=> FAIL - " + msg)
    mt5.shutdown()
    sys.exit(1)


print("=" * 60)
print("ORDER-PATH SMOKE TEST (opens 1 minimal order, closes it at once)")
print("=" * 60)

if not mt5.initialize(timeout=15_000):
    print("initialize failed: %s" % (mt5.last_error(),))
    print("=> run check.py first - this test assumes attach already works")
    sys.exit(1)

account = mt5.account_info()
if account is None:
    die("no account logged in - log in to the DEMO account in the terminal")
if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
    die("account %s is NOT a demo account (trade_mode=%s) - refusing to test on real money"
        % (account.login, account.trade_mode))
print("account     : %s @ %s (DEMO)  balance=%s %s"
      % (account.login, account.server, account.balance, account.currency))

term = mt5.terminal_info()
print("AutoTrading : %s  (must be True, toolbar 'Algo Trading' button)" % term.trade_allowed)

mt5.symbol_select(SYMBOL, True)
info = mt5.symbol_info(SYMBOL)
if info is None:
    die("symbol %s not available - add it in Market Watch (Ctrl+M)" % SYMBOL)
tick = mt5.symbol_info_tick(SYMBOL)
if tick is None or tick.ask == 0:
    die("no tick for %s - market closed or feed down, re-run when quotes move" % SYMBOL)

volume = max(info.volume_min, 0.01)
# SL/TP distance: comfortably beyond the broker stops-level requirement
dist = max(info.trade_stops_level * 3, 500) * info.point
print("symbol      : %s  tick=%s  volume_min=%s  stops_level=%s pts"
      % (SYMBOL, tick.ask, info.volume_min, info.trade_stops_level))

# identical shape to runner/main.py send_order (BUY branch)
request = {
    "action": mt5.TRADE_ACTION_DEAL,
    "symbol": SYMBOL,
    "volume": volume,
    "type": mt5.ORDER_TYPE_BUY,
    "price": tick.ask,
    "sl": tick.ask - dist,
    "tp": tick.ask + dist,
    "deviation": 20,
    "magic": MAGIC,
    "comment": "order-path-smoke-test",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC,
}
print("\nsending BUY %s %s lots (sl/tp attached, magic=%d)..." % (SYMBOL, volume, MAGIC))
result = mt5.order_send(request)
if result is None:
    die("order_send returned None: %s" % (mt5.last_error(),))
if result.retcode != mt5.TRADE_RETCODE_DONE:
    hint = HINTS.get(result.retcode, "send this whole output to Claude")
    print("retcode     : %s  comment=%s" % (result.retcode, result.comment))
    die("order REJECTED -> " + hint)
print("OPEN  OK    : ticket=%s price=%s" % (result.order, result.price))

# close it right away (opposite deal against the position ticket)
positions = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
if not positions:
    die("order filled but position not found - check terminal manually (magic=%d)" % MAGIC)
pos = positions[0]
tick = mt5.symbol_info_tick(SYMBOL)
close = mt5.order_send({
    "action": mt5.TRADE_ACTION_DEAL,
    "symbol": SYMBOL,
    "volume": pos.volume,
    "type": mt5.ORDER_TYPE_SELL,
    "price": tick.bid,
    "deviation": 20,
    "magic": MAGIC,
    "position": pos.ticket,
    "comment": "smoke-test-close",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC,
})
if close is None or close.retcode != mt5.TRADE_RETCODE_DONE:
    print("CLOSE FAILED: %s" % (close._asdict() if close else mt5.last_error(),))
    print("=> PARTIAL PASS - opening works; CLOSE the position MANUALLY in the terminal (magic=%d)" % MAGIC)
else:
    print("CLOSE OK    : price=%s" % close.price)
    print("\n=> PASS - full order path works (cost: one spread on %s lots)." % pos.volume)
    print("   Runner will trade the same way when a signal fires.")

mt5.shutdown()
