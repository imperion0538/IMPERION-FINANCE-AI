import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("IMPERION_STORAGE_DIR", "/tmp/imperion-test-storage")
for k in ("MODE", "LIVE_TRADING_CONFIRM", "BINANCE_API_KEY", "BINANCE_API_SECRET", "KILL_SWITCH"):
    os.environ.pop(k, None)

from imperion.fakes import stop_all_engines  # noqa: E402


@pytest.fixture(autouse=True)
def no_leaked_threads():
    """P0 guard: every test must leave no thread behind (engines, sockets, HTTP servers, workers)."""
    before = set(threading.enumerate())
    yield
    stuck = stop_all_engines()
    assert not stuck, f"engine threads did not stop: {stuck}"
    deadline = time.time() + 5.0
    leaked = []
    while time.time() < deadline:
        leaked = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        if not leaked:
            break
        time.sleep(0.05)
    assert not leaked, f"threads leaked by this test: {[t.name for t in leaked]}"
