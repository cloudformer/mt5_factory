"""Boot self-test - the Windows-side equivalent of `make up` API checks.

Runs automatically at login (startup shortcut, after bridge/runner start):
waits for the bridge to become healthy (MT5 launch + login can take minutes
at boot), then exercises the whole chain through real HTTP calls:

  1 bridge-port   8020 responds
  2 mt5-account   MT5 connected + account logged in
  3 algo-trading  terminal trade permission (order path precondition)
  4 runner        runner process alive, role + strategy count
  5 quotes        no skipped symbols; ticks fresh (stale = market closed or feed down)
  6 order-test    POST /ordertest round-trip (DEMO account only, auto-SKIP otherwise)
  7 recon         GET /recon totals readable
  8 cross-check   per-strategy stats (conn/stats.py -> web page) vs /recon (independent
                  read of MT5) must agree per magic - catches bugs in either path

Result -> selftest_result.json (bridge status page shows it: no Windows login needed).
Exit 0 = no FAIL. WARN/SKIP do not fail the run. English output (GBK console).
"""
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / "env" / ".dev.env")
PORT = int(os.getenv("MT5_PORT", "8020"))
BASE = f"http://127.0.0.1:{PORT}"
RESULT_FILE = Path(__file__).resolve().parent / "selftest_result.json"
WAIT_HEALTHY_S = 600   # boot: bridge must launch terminal + login, be patient
WAIT_RUNNER_S = 150    # runner refreshes strategies every 60s after attach
QUOTE_STALE_S = 600

checks: list = []


def check(name: str, status: str, detail: str = "") -> None:
    checks.append({"name": name, "status": status, "detail": str(detail)[:200]})
    print("%-5s %-13s %s" % (status, name, detail))


def get_health() -> dict | None:
    try:
        return requests.get(f"{BASE}/health", timeout=5).json()
    except (requests.RequestException, ValueError):
        return None


def finish(t0: float) -> None:
    ok = not any(c["status"] == "FAIL" for c in checks)
    result = {"updated": time.time(), "ok": ok, "took_s": round(time.time() - t0),
              "checks": checks}
    try:
        RESULT_FILE.write_text(json.dumps(result))
    except OSError as e:
        print("cannot write result file: %s" % e)
    print("-" * 60)
    print("=> %s (%d checks, %ds) - also shown on the status page http://localhost:%d/"
          % ("ALL OK" if ok else "FAIL", len(checks), result["took_s"], PORT))
    sys.exit(0 if ok else 1)


print("=" * 60)
print("WORKER SELF-TEST (waits for services, then tests the full chain)")
print("=" * 60)
t0 = time.time()

# 1+2: bridge port, then healthy (MT5 + account)
health, port_ok = None, False
while time.time() - t0 < WAIT_HEALTHY_S:
    h = get_health()
    if h is not None:
        if not port_ok:
            port_ok = True
            check("bridge-port", "PASS", "port %d responding (%ds)" % (PORT, time.time() - t0))
        health = h
        if h.get("status") == "healthy":
            break
    time.sleep(10)
if not port_ok:
    check("bridge-port", "FAIL", "no response on %d after %ds - check start_bridge.bat window"
          % (PORT, WAIT_HEALTHY_S))
    finish(t0)
if health.get("status") != "healthy":
    check("mt5-account", "FAIL", "not healthy after %ds - terminal dialog stuck? no account? "
          "(push one from web Workers page)" % WAIT_HEALTHY_S)
    finish(t0)
check("mt5-account", "PASS", "%s @ %s" % (health.get("login"), health.get("server")))

# 3: algo trading switch (order path precondition; baked into terminal_start.ini)
if health.get("trade_allowed"):
    check("algo-trading", "PASS", "trade allowed")
else:
    check("algo-trading", "FAIL", "AutoTrading OFF - terminal launched manually? "
          "click the Algo Trading toolbar button")

# 4: runner alive (role may legitimately be unassigned on a download-only host)
deadline = time.time() + WAIT_RUNNER_S
runner = {}
while time.time() < deadline:
    runner = (get_health() or {}).get("runner") or {}
    if runner.get("alive") and runner.get("strategies") is not None:
        break
    time.sleep(10)
if not runner.get("alive"):
    check("runner", "FAIL", "not running after %ds - check start_runner.bat window" % WAIT_RUNNER_S)
else:
    check("runner", "PASS", "role=%s strategies=%s"
          % (runner.get("run_status") or "-", runner.get("strategies")))

# 5: quotes for loaded strategies (skipped = symbol missing at this broker)
skipped = runner.get("skipped") or []
per_strategy = runner.get("per_strategy") or []
if skipped:
    check("quotes", "FAIL", "no quotes for: %s (add in Market Watch / wrong symbol name)"
          % ",".join(sorted({s["symbol"] for s in skipped})))
elif not per_strategy:
    check("quotes", "SKIP", "no strategies loaded (role unassigned or none in this status)")
else:
    now = time.time()
    stale = sorted({s.get("symbol") or s["name"] for s in per_strategy
                    if s.get("quote_ts") and now - s["quote_ts"] > QUOTE_STALE_S})
    if stale:
        check("quotes", "WARN", "stale ticks: %s (market closed? feed down?)" % ",".join(stale))
    else:
        check("quotes", "PASS", "%d strategies, all ticks fresh" % len(per_strategy))

# 6: order round-trip (bridge hard-refuses REAL accounts -> SKIP on live hosts)
# 环境性拒单(休市/无报价)= SKIP 不是 FAIL: 周末休市不该报红, 只有链路本身坏才 FAIL。
MARKET_CLOSED = {10018, 10019}  # retcode: 市场关闭 / 报价不足
try:
    r = requests.post(f"{BASE}/ordertest", timeout=60)
    body = r.json()
    detail = body.get("detail")
    retcode = detail.get("retcode") if isinstance(detail, dict) else None
    if r.status_code == 200 and body.get("result") == "PASS":
        check("order-test", "PASS", "open+close round-trip ok")
    elif r.status_code == 200:
        check("order-test", "WARN", body)
    elif r.status_code == 403:
        check("order-test", "SKIP", "real account - test order refused by design")
    elif r.status_code == 400 or retcode in MARKET_CLOSED:
        check("order-test", "SKIP", detail)  # 休市 / 无报价 — 非故障, 开盘后自然能测
    else:
        check("order-test", "FAIL", detail)
except (requests.RequestException, ValueError) as e:
    check("order-test", "FAIL", e)

# 7: reconciliation data readable
try:
    rec = requests.get(f"{BASE}/recon", params={"fmt": "json"}, timeout=60).json()
    t = rec["strategy_totals"]
    check("recon", "PASS", "closed=%s realized=%s (match web page sums)"
          % (t["closed"], t["realized"]))
except (requests.RequestException, ValueError, KeyError) as e:
    check("recon", "FAIL", e)

# 8: cross-check - same numbers via two INDEPENDENT code paths must agree per magic:
#    conn/stats.py (feeds the web page) vs bridge /recon (reads MT5 directly).
#    Would have caught the stats timezone bug automatically.
try:
    ps = ((get_health() or {}).get("runner") or {}).get("per_strategy") or []

    def _mismatches():
        by_magic = {c["magic"]: c for c in rec["closed_by_magic"]}
        bad = []
        for s in ps:
            c = by_magic.get(s["magic"], {"out": 0, "pnl": 0.0})
            if (s["closed"]["trades"] != c["out"]
                    or abs(s["closed"]["profit"] - c["pnl"]) > 0.011):
                bad.append("magic %s: stats %d/%.2f vs recon %d/%.2f"
                           % (s["magic"], s["closed"]["trades"], s["closed"]["profit"],
                              c["out"], c["pnl"]))
        return bad

    if not ps:
        check("cross-check", "SKIP", "no strategies loaded")
    else:
        bad = _mismatches()
        if bad:  # stats has a 60s cache: a trade closing in between skews transiently -
                 # retry once after the cache expires before declaring failure
            time.sleep(70)
            rec = requests.get(f"{BASE}/recon", params={"fmt": "json"}, timeout=60).json()
            ps = ((get_health() or {}).get("runner") or {}).get("per_strategy") or []
            bad = _mismatches()
        if bad:
            check("cross-check", "FAIL", "code paths disagree: " + "; ".join(bad[:3]))
        else:
            check("cross-check", "PASS", "%d strategies: stats == recon" % len(ps))
except Exception as e:
    check("cross-check", "SKIP", "recon unavailable: %s" % e)

finish(t0)
