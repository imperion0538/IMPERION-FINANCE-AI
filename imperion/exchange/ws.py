"""Binance USDⓈ-M WebSocket market streams.

Since 2026 the legacy `/ws` and `/stream` URLs are gone; streams live under
  wss://fstream.binance.com/market  -> aggTrade, kline, markPrice
  wss://fstream.binance.com/public  -> depth, bookTicker
We connect to `<base>/<path>/stream` and SUBSCRIBE in small batches.
The `websocket-client` package is imported lazily; if it is missing the stream reports
`available=False` and the health gate blocks trading (fail-closed).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from ..util import now_ms
from ..data.microstructure import BookStore, TradeStore

log = logging.getLogger("imperion.ws")

MAX_LIFETIME_S = 23 * 3600  # Binance drops connections at 24h


def _default_factory(url, on_open, on_message, on_error, on_close):
    import websocket  # type: ignore

    return websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)


class StreamConnection:
    def __init__(self, name: str, url: str, on_payload: Callable[[dict], None], factory: Optional[Callable] = None):
        self.name, self.url, self.on_payload = name, url, on_payload
        self._factory = factory or _default_factory
        self._streams: Set[str] = set()
        self._subscribed: Set[str] = set()
        self._app: Any = None
        self._thread: Optional[threading.Thread] = None
        self._helpers: List[threading.Thread] = []      # watchdog / subscription threads, joined on stop()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._msg_id = 0
        self.connected = False
        self.connected_at = 0.0
        self.last_msg_ms = 0
        self.reconnects = 0
        self.available = True
        self.last_error = ""

    # ---- public --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            if self._factory is _default_factory:
                import websocket  # noqa: F401  # type: ignore
        except ImportError:
            self.available = False
            self.last_error = "websocket-client package not installed"
            log.error("websocket-client is not installed; live microstructure data disabled")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"ws-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        for t in [self._thread, *self._helpers]:
            if t is not None and t is not threading.current_thread():
                t.join(timeout=5)
        self._helpers = [t for t in self._helpers if t.is_alive()]

    def set_streams(self, streams: Iterable[str]) -> None:
        with self._lock:
            self._streams = set(streams)
        self._sync_subscriptions()

    # ---- internals -----------------------------------------------------
    def _send(self, method: str, params: List[str]) -> None:
        with self._lock:
            app = self._app
            self._msg_id += 1
            mid = self._msg_id
        if app is None or not params:
            return
        app.send(json.dumps({"method": method, "params": params, "id": mid}))

    def _sync_subscriptions(self) -> None:
        if not self.connected:
            return
        with self._lock:
            add = sorted(self._streams - self._subscribed)
            remove = sorted(self._subscribed - self._streams)
        try:
            for i in range(0, len(remove), 40):
                self._send("UNSUBSCRIBE", remove[i:i + 40]); time.sleep(0.25)
            for i in range(0, len(add), 40):
                self._send("SUBSCRIBE", add[i:i + 40]); time.sleep(0.25)
            with self._lock:
                self._subscribed = set(self._streams)
        except Exception as exc:
            log.warning("[%s] subscription sync failed: %s", self.name, exc)

    def _on_open(self, _ws) -> None:
        self.connected, self.connected_at = True, time.time()
        with self._lock:
            self._subscribed = set()
        log.info("[%s] connected", self.name)
        self._spawn(self._sync_subscriptions, f"ws-{self.name}-subscribe")

    def _on_message(self, _ws, raw) -> None:
        self.last_msg_ms = now_ms()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict):
            try:
                self.on_payload(payload)
            except Exception:  # never let a bad message kill the socket thread
                log.exception("[%s] handler error", self.name)

    def _on_error(self, _ws, err) -> None:
        self.last_error = str(err)[:200]
        log.warning("[%s] error: %s", self.name, self.last_error)

    def _on_close(self, _ws, code=None, msg=None) -> None:
        self.connected = False
        log.info("[%s] closed (%s)", self.name, code)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            app = self._factory(self.url, self._on_open, self._on_message, self._on_error, self._on_close)
            with self._lock:
                self._app = app
            started = time.time()
            self._spawn(lambda app=app: self._lifetime_watchdog(app), f"ws-{self.name}-watchdog")
            try:
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self.last_error = str(exc)[:200]
            self.connected = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            backoff = 1.0 if time.time() - started > 60 else min(backoff * 2, 30.0)
            log.warning("[%s] reconnecting in %.0fs", self.name, backoff)
            self._stop.wait(backoff)

    def _spawn(self, fn: Callable[[], None], name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        with self._lock:
            self._helpers = [h for h in self._helpers if h.is_alive()] + [t]
        t.start()

    def _lifetime_watchdog(self, app) -> None:
        while not self._stop.wait(5.0):            # wakes immediately on stop(): never outlives the connection
            if self._app is not app:
                return
            if self.connected and time.time() - self.connected_at > MAX_LIFETIME_S:
                try:
                    app.close()
                except Exception:
                    pass
                return

    def fresh(self, max_age_ms: int = 60_000) -> bool:
        return self.connected and now_ms() - self.last_msg_ms <= max_age_ms


class MarketStreams:
    """Routes aggTrade + partial depth into the stores for fixed and radar symbols."""

    DEPTH = "depth20@500ms"

    def __init__(self, ws_base: str, trades: TradeStore, books: BookStore, factory: Optional[Callable] = None):
        self.trades, self.books = trades, books
        self.market = StreamConnection("market", f"{ws_base}/market/stream", self.handle, factory)
        self.public = StreamConnection("public", f"{ws_base}/public/stream", self.handle, factory)
        self.symbols: List[str] = []

    def start(self, symbols: Iterable[str]) -> None:
        self.market.start()
        self.public.start()
        self.set_symbols(symbols)

    def set_symbols(self, symbols: Iterable[str]) -> None:
        self.symbols = list(dict.fromkeys(s.upper() for s in symbols))
        low = [s.lower() for s in self.symbols]
        self.market.set_streams(f"{s}@aggTrade" for s in low)
        self.public.set_streams(f"{s}@{self.DEPTH}" for s in low)

    def stop(self) -> None:
        self.market.stop()
        self.public.stop()

    @property
    def available(self) -> bool:
        return self.market.available and self.public.available

    def healthy(self) -> bool:
        return self.market.fresh(120_000) and self.public.fresh(30_000)

    def status(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "market_connected": self.market.connected, "public_connected": self.public.connected,
            "market_age_s": round((now_ms() - self.market.last_msg_ms) / 1000, 1) if self.market.last_msg_ms else None,
            "public_age_s": round((now_ms() - self.public.last_msg_ms) / 1000, 1) if self.public.last_msg_ms else None,
            "reconnects": self.market.reconnects + self.public.reconnects,
            "last_error": self.market.last_error or self.public.last_error,
        }

    def handle(self, payload: dict) -> None:
        """Process one combined-stream message ({'stream':..., 'data': {...}})."""
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return
        event = data.get("e")
        if event == "aggTrade":
            self.trades.add(str(data["s"]), int(data["T"]), float(data["p"]), float(data["q"]), bool(data["m"]))
        elif event == "depthUpdate" or ("b" in data and "a" in data and "s" in data):
            self.books.update(str(data["s"]), data.get("b", []), data.get("a", []), data.get("E"))
