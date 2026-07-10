"""Reconciliation dump - READ-ONLY, never trades.

Groups MT5 history (last N days, default 90) by magic number so every number
on the web Demo/Live page can be matched line by line:
  web "trades"          = OUT rows here (closing legs, full closes)
  web "realized profit" = profit+commission+swap summed over OUT rows
  web ignores           : magic 0 (manual/deposit), 999999 (smoke test), open positions

Also prints raw counts (positions/orders/deals) - the MT5 History tab shows one
of these depending on its right-click view mode, which is the usual source of
"the numbers don't match" confusion.

Run via recon.bat, or: python recon.py [days]
English-only output: Windows console codepage (GBK) garbles Chinese.
"""
import sys
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
SMOKE_MAGIC = 999999

if not mt5.initialize(timeout=15_000):
    print("initialize failed: %s -> run check.py first" % (mt5.last_error(),))
    sys.exit(1)
acct = mt5.account_info()
if acct is None:
    print("no account logged in")
    sys.exit(1)

# +1 day buffer: history filter runs on server time which is hours off UTC
now = datetime.now(timezone.utc)
deals = mt5.history_deals_get(now - timedelta(days=DAYS), now + timedelta(days=1)) or []
orders = mt5.history_orders_get(now - timedelta(days=DAYS), now + timedelta(days=1)) or []
positions = mt5.positions_get() or []

by_magic: dict = {}
balance_rows = 0
for d in deals:
    if d.type == mt5.DEAL_TYPE_BALANCE:      # deposits/withdrawals
        balance_rows += 1
        continue
    s = by_magic.setdefault(d.magic, {"in": 0, "out": 0, "wins": 0, "profit": 0.0})
    if d.entry == mt5.DEAL_ENTRY_IN:
        s["in"] += 1
    elif d.entry == mt5.DEAL_ENTRY_OUT:      # closing leg: pnl lands here (same as web)
        s["out"] += 1
        pnl = d.profit + d.commission + d.swap
        s["profit"] += pnl
        if pnl > 0:
            s["wins"] += 1

open_by_magic: dict = {}
for p in positions:
    o = open_by_magic.setdefault(p.magic, {"count": 0, "volume": 0.0, "profit": 0.0})
    o["count"] += 1
    o["volume"] += p.volume
    o["profit"] += p.profit

def note(magic: int) -> str:
    if magic == 0:
        return "manual / non-strategy"
    if magic == SMOKE_MAGIC:
        return "ordertest smoke test"
    if 100_000 <= magic < 200_000:
        return "strategy id %d" % (magic - 100_000)
    return "?"

print("=" * 74)
print("RECONCILIATION  account=%s @ %s  window=last %d days" % (acct.login, acct.server, DAYS))
print("=" * 74)
print("CLOSED (compare each row to the web page: trades / win% / realized pnl)")
print("%-8s %4s %4s %5s %10s   %s" % ("magic", "in", "out", "wins", "pnl", "note"))
strategy_out = strategy_pnl = 0.0
for magic in sorted(by_magic):
    s = by_magic[magic]
    print("%-8s %4d %4d %5d %10.2f   %s"
          % (magic, s["in"], s["out"], s["wins"], s["profit"], note(magic)))
    if 100_000 <= magic < 200_000:
        strategy_out += s["out"]
        strategy_pnl += s["profit"]
print("-" * 74)
print("strategy totals: closed=%d  realized=%.2f   <- web page column sums must equal these"
      % (strategy_out, strategy_pnl))

print()
print("OPEN POSITIONS (web shows these under holding/floating, NOT in trades)")
for magic in sorted(open_by_magic):
    o = open_by_magic[magic]
    print("%-8s %d pos  %.2f lots  floating %+.2f   %s"
          % (magic, o["count"], o["volume"], o["profit"], note(magic)))
if not open_by_magic:
    print("(none)")

print()
print("RAW COUNTS (what the MT5 History tab shows depends on its view mode)")
closed_positions = sum(s["out"] for s in by_magic.values())
print("  Positions view : %d closed positions (+%d balance rows hidden)"
      % (closed_positions, balance_rows))
print("  Orders view    : %d orders" % len(orders))
print("  Deals view     : %d deals (incl %d balance/deposit rows)" % (len(deals), balance_rows))
print("  Open now       : %d positions (Trade tab, never in History)" % len(positions))

mt5.shutdown()
