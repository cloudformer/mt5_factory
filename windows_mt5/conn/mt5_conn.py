"""MT5 connection primitive - the ONE fragile step, isolated and frozen.

Mirrors windows_mt5/test/check.py (the verified-good baseline). ATTACH-ONLY:
connects to a terminal a human (or Windows startup) already opened and logged in.
It does NOT launch the terminal, set MT5_PATH, or kill/relaunch on failure - that
machinery is exactly what broke bring-up for a night. Future feature work must NOT
edit this file; change connection behaviour here only, mirroring test/check.py.

Usage:
    import mt5_conn
    mt5_conn.attach()                         # attach to the logged-in terminal
    mt5_conn.attach({"login": .., "password": .., "server": ..})   # or with creds
    if mt5_conn.is_connected(): ...
    with mt5_conn.lock():                      # around any direct mt5.* data/trade call
        mt5.copy_rates_range(...)
"""
import threading
from typing import Optional

import MetaTrader5 as mt5

_lock = threading.Lock()          # MetaTrader5 package is NOT thread-safe
_connected = False
_account_cache: Optional[dict] = None   # last good account, for non-blocking status


def attach(creds: Optional[dict] = None) -> bool:
    """Attach to a running, logged-in terminal.
    creds = {"login": int, "password": str, "server": str} or None (use logged-in account).
    Returns True only when IPC works AND an account is present (mirrors check.py)."""
    global _connected
    with _lock:
        mt5.shutdown()                        # clean slate; safe even if not initialized
        kwargs = dict(creds) if creds else {}
        kwargs["timeout"] = 15000             # fail fast; the reconnect loop retries
        if not mt5.initialize(**kwargs):      # <-- the one fragile call, same as check.py
            _connected = False
            return False
        _connected = mt5.account_info() is not None
        return _connected


def is_connected() -> bool:
    """Cheap last-known state (no IPC)."""
    return _connected


def is_alive() -> bool:
    """Live check: still attached and terminal responsive (brief lock)."""
    with _lock:
        return _connected and mt5.terminal_info() is not None


def drop() -> None:
    """Mark disconnected; the reconnect loop will re-attach."""
    global _connected
    _connected = False


def snapshot(timeout: float = 2.0):
    """Non-blocking status for /health and the status page -> (connected, account|None).
    If the lock is held (a 15s attach, or a big rates pull), return the cached account
    instead of blocking: the status endpoint must NEVER freeze or the app marks it OFFLINE."""
    global _account_cache
    if _lock.acquire(timeout=timeout):
        try:
            info = mt5.account_info() if _connected else None
            if info is not None:
                _account_cache = info._asdict()
                return True, _account_cache
            return False, None
        finally:
            _lock.release()
    return _connected, (_account_cache if _connected else None)


def last_error():
    with _lock:
        return mt5.last_error()


def lock() -> threading.Lock:
    """Shared lock. Wrap ANY direct mt5.* call (rates/orders) in `with mt5_conn.lock():`."""
    return _lock
