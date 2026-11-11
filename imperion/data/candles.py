"""Closed-candle store + REST bootstrap/poller.

Candles come from REST (authoritative, includes taker-buy volume => per-candle delta for every
timeframe). WebSocket is used only for trade/depth microstructure, see data/microstructure.py.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

from ..models import Candle
from ..util import last_closed_open, now_ms, tf_ms

log = logging.getLogger("imperion.candles")

MAX_CANDLES = 1000
MIN_HISTORY = {"1w": 30}
DEFAULT_MIN_HISTORY = 100


class CandleStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed: Dict[Tuple[str, str], List[Candle]] = {}
        self._forming: Dict[Tuple[str, str], Candle] = {}

    def ingest(self, symbol: str, tf: str, rows: Iterable[list], now: Optional[int] = None) -> int:
        """Merge REST kline rows. Returns number of NEW closed candles."""
        now = now if now is not None else now_ms()
        added = 0
        with self._lock:
            cur = self._closed.setdefault((symbol, tf), [])
            known = {c.t: i for i, c in enumerate(cur)}
            for row in rows:
                try:
                    c = Candle.from_rest(row)
                except (IndexError, TypeError, ValueError):
                    continue
                if c.ct >= now:               # still forming
                    self._forming[(symbol, tf)] = c
                    continue
                if c.t in known:
                    cur[known[c.t]] = c
                else:
                    cur.append(c)
                    added += 1
            cur.sort(key=lambda x: x.t)
            if len(cur) > MAX_CANDLES:
                del cur[: len(cur) - MAX_CANDLES]
        return added

    def get(self, symbol: str, tf: str, n: Optional[int] = None) -> List[Candle]:
        with self._lock:
            data = self._closed.get((symbol, tf), [])
            return list(data[-n:] if n else data)

    def last_open(self, symbol: str, tf: str) -> Optional[int]:
        with self._lock:
            data = self._closed.get((symbol, tf))
            return data[-1].t if data else None

    def forming(self, symbol: str, tf: str) -> Optional[Candle]:
        with self._lock:
            return self._forming.get((symbol, tf))

    def count(self, symbol: str, tf: str) -> int:
        with self._lock:
            return len(self._closed.get((symbol, tf), []))

    def has_gap(self, symbol: str, tf: str, n: int = 40) -> bool:
        step = tf_ms(tf)
        with self._lock:
            data = self._closed.get((symbol, tf), [])[-n:]
        return any(b.t - a.t != step for a, b in zip(data, data[1:]))

    def last_price(self, symbol: str) -> Optional[float]:
        fm = self.forming(symbol, "1m")
        if fm:
            return fm.c
        cs = self.get(symbol, "1m", 1)
        return cs[-1].c if cs else None


class CandlePoller:
    """Fetches candles when a new one should have closed. Weight cost is a handful of requests/min."""

    def __init__(self, rest, store: CandleStore, symbols: Iterable[str], timeframes: Iterable[str]):
        self.rest, self.store = rest, store
        self.symbols, self.timeframes = list(symbols), list(timeframes)
        self._last_attempt: Dict[Tuple[str, str], float] = {}

    def bootstrap(self, limit: int = 500, stop: Optional[threading.Event] = None) -> int:
        """Initial history for every (symbol, timeframe). Returns early when `stop` is set (shutdown during startup)."""
        loaded = 0
        for sym in self.symbols:
            for tf in self.timeframes:
                if stop is not None and stop.is_set():
                    return loaded
                try:
                    rows = self.rest.klines(sym, tf, limit=limit)
                    self.store.ingest(sym, tf, rows)
                    loaded += 1
                except Exception as exc:
                    log.warning("bootstrap %s %s failed: %s", sym, tf, exc)
        return loaded

    def poll_due(self, now: Optional[int] = None) -> List[Tuple[str, str]]:
        """Return (symbol, tf) pairs that received a NEW closed candle."""
        now = now if now is not None else now_ms()
        updated: List[Tuple[str, str]] = []
        for tf in self.timeframes:
            expected = last_closed_open(now, tf)
            step = tf_ms(tf)
            if now < expected + step + 800:      # give the exchange a moment to finalise
                continue
            for sym in self.symbols:
                have = self.store.last_open(sym, tf)
                if have is not None and have >= expected:
                    continue
                key = (sym, tf)
                if time.time() - self._last_attempt.get(key, 0) < 2.0:
                    continue
                self._last_attempt[key] = time.time()
                missing = 3 if have is None else int((expected - have) // step) + 2
                try:
                    rows = self.rest.klines(sym, tf, limit=max(3, min(missing, 200)))
                    if self.store.ingest(sym, tf, rows, now=now) > 0:
                        updated.append(key)
                except Exception as exc:
                    log.debug("poll %s %s failed: %s", sym, tf, exc)
        return updated
