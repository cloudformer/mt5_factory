"""MT5 connection self-test - bypasses ALL project code (bridge/runner/env).
Answers one question: can Python attach to MT5 on this machine?
Run via check.bat (normal privilege). Prereq: MT5 terminal open + logged in.
English-only output: the Windows console codepage (GBK) garbles Chinese.

Only the VERDICT block matters - it is self-contained. Copy JUST that block to Claude.
"""
import ctypes
import struct
import sys

bits = struct.calcsize("P") * 8
pyver = sys.version.split()[0]

try:
    import MetaTrader5 as mt5
    pkg = mt5.__version__
    imported = True
except ImportError:
    pkg = "NOT INSTALLED"
    imported = False

try:
    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
except Exception:
    admin = "?"

ok = err = account = None
if imported:
    ok = mt5.initialize()          # the one fragile step: attach to a running terminal
    err = mt5.last_error()
    account = mt5.account_info()

# ---- decide verdict ----
if not imported:
    result = "FAIL - MetaTrader5 not installed (run: python -m pip install MetaTrader5)"
elif ok and account is not None:
    result = ("OK - core works. This machine CAN do it; problem is our orchestration.\n"
              "         -> Tell Claude: simplify bridge (attach-only, no auto-launch/self-heal).\n"
              "         account=%s @ %s  balance=%s %s"
              % (account.login, account.server, account.balance, account.currency))
elif ok and account is None:
    result = ("IPC OK but NO ACCOUNT logged in.\n"
              "         -> Log into your demo account inside the MT5 terminal, then re-run.")
elif err and err[0] == -10005:
    result = ("FAIL -10005 IPC timeout = almost always PRIVILEGE MISMATCH.\n"
              "         -> This window must be NORMAL (not admin), MT5 opened by normal double-click.")
else:
    result = "FAIL initialize error %s -> send this whole block to Claude." % (err,)

print("\n" + "=" * 60)
print("VERDICT (copy this whole block)")
print("=" * 60)
print("python      : %s  %s-bit  (need 64)" % (pyver, bits))
print("MetaTrader5 : %s" % pkg)
print("admin       : %s  (should be False)" % admin)
print("initialize  : %s" % ok)
print("last_error  : %s" % (err,))
print("account     : %s" % (account._asdict() if account else None))
print("-" * 60)
print("=> " + result)
print("=" * 60)

if imported:
    mt5.shutdown()
