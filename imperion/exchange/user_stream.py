"""Private user-data stream (order / position events).

Role: an ACCELERATOR, never a source of truth. An order or account event only wakes the guardian so it reconciles
through REST immediately instead of waiting for the next 4 s cycle. If the stream is down, mis-configured or the
endpoint changes, nothing is lost: the guardian keeps polling exactly as before.

Flow: POST /fapi/v1/listenKey (API key only) -> WebSocket <ws_base>/<path>/<listenKey> -> PUT keep-alive every 30 min.
The WebSocket path is configurable (USER_STREAM_PATH) because Binance has moved its stream URLs before; verify it on
Testnet before relying on the faster reaction time.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from .ws import StreamConnection

log = logging.getLogger("imperion.userstream")

KEEPALIVE_S = 30 * 60
WAKE_EVENTS = ("ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE", "ALGO_UPDATE", "CONDITIONAL_ORDER_TRIGGER_REJECT")


class UserStream:
    def __init__(self, rest, ws_base: str, path: str, on_wake: Callable[[str], None], factory: Optional[Callable] = None):
        self.rest, self.ws_base, self.path, self.on_wake, self._factory = rest, ws_base.rstrip("/"), path.strip("/"), on_wake, factory
        self.conn: Optional[StreamConnection] = None
        self.listen_key = ""
        self.events = 0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _new_key(self) -> str:
        data = self.rest.request("POST", "/fapi/v1/listenKey", priority=True, retries=0)
        key = str(data.get("listenKey") or "")
        if not key:
            raise ValueError("listenKey missing in response")
        return key

    def start(self) -> bool:
        """Returns False (and stays off) when the listen key cannot be created."""
        try:
            self.listen_key = self._new_key()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("user stream disabled, listenKey failed: %s", self.last_error)
            return False
        self._connect()
        self._stop.clear()
        self._thread = threading.Thread(target=self._keepalive, name="user-stream-keepalive", daemon=True)
        self._thread.start()
        return True

    def _connect(self) -> None:
        if self.conn is not None:
            self.conn.stop()
        self.conn = StreamConnection("user", f"{self.ws_base}/{self.path}/{self.listen_key}", self.handle, self._factory)
        self.conn.start()

    def _keepalive(self) -> None:
        while not self._stop.wait(KEEPALIVE_S):
            try:
                self.rest.request("PUT", "/fapi/v1/listenKey", priority=True, retries=0)
            except Exception as exc:
                self.last_error = f"keepalive: {type(exc).__name__}: {exc}"
                log.warning("user stream keep-alive failed: %s", exc)

    def handle(self, payload: Dict[str, Any]) -> None:
        data = payload.get("data", payload)
        event = data.get("e") if isinstance(data, dict) else None
        if event == "listenKeyExpired":
            try:
                self.listen_key = self._new_key()
                threading.Thread(target=self._connect, name="user-stream-reconnect", daemon=True).start()   # never restart a socket from its own thread
            except Exception as exc:
                self.last_error = f"renew: {type(exc).__name__}: {exc}"
            return
        if event in WAKE_EVENTS:
            self.events += 1
            self.on_wake(str(event))

    def stop(self) -> None:
        self._stop.set()
        if self.conn is not None:
            self.conn.stop()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def status(self) -> Dict[str, Any]:
        return {"connected": bool(self.conn and self.conn.connected), "events": self.events, "last_error": self.last_error or (self.conn.last_error if self.conn else "")}
