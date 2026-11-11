"""Trade-flow and order-book state fed by WebSocket (aggTrade + partial depth).

Semantics: in an aggTrade event `m == True` means the BUYER is the maker, i.e. the aggressor SOLD.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from ..util import now_ms

# Relative whale floor per symbol (multiplied by WHALE_FLOOR_USDT). Larger caps -> larger absolute size.
FLOOR_MULT = {"BTCUSDT": 3.0, "ETHUSDT": 2.0, "SOLUSDT": 1.2, "XRPUSDT": 1.0}
DEFAULT_FLOOR_MULT = 0.6
BUCKET_MS = 60_000
BUCKET_KEEP = 360  # minutes


@dataclass
class Bucket:
    minute: int
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    buy_usdt: float = 0.0
    sell_usdt: float = 0.0
    n: int = 0
    levels: Dict[int, List[float]] = field(default_factory=dict)  # bin -> [buy_qty, sell_qty]

    @property
    def delta_qty(self) -> float:
        return self.buy_qty - self.sell_qty


class _Sym:
    def __init__(self, floor: float):
        self.floor = floor
        self.trades: Deque[Tuple[int, float, float, bool]] = deque(maxlen=60_000)
        self.notionals: Deque[float] = deque(maxlen=3000)
        self.whales: Deque[Tuple[int, float, float, bool]] = deque(maxlen=3000)  # ts, usdt, price, aggressor_buy
        self.buckets: Dict[int, Bucket] = {}
        self.bin_size: Optional[float] = None
        self.threshold = floor
        self.since_recalc = 0
        self.last_ts = 0
        self.last_price = 0.0


def _two_sig(x: float) -> float:
    if x <= 0:
        return 1e-9
    mag = 10 ** math.floor(math.log10(x))
    return round(x / mag, 1) * mag if x / mag >= 1 else mag


class TradeStore:
    def __init__(self, whale_floor_usdt: float = 50_000.0):
        self.base_floor = whale_floor_usdt
        self._lock = threading.RLock()
        self._s: Dict[str, _Sym] = {}

    def _sym(self, symbol: str) -> _Sym:
        st = self._s.get(symbol)
        if st is None:
            st = self._s[symbol] = _Sym(self.base_floor * FLOOR_MULT.get(symbol, DEFAULT_FLOOR_MULT))
        return st

    def add(self, symbol: str, ts: int, price: float, qty: float, buyer_is_maker: bool) -> None:
        if not (price > 0 and qty > 0):
            return
        usdt = price * qty
        aggressor_buy = not buyer_is_maker
        with self._lock:
            st = self._sym(symbol)
            st.trades.append((ts, price, qty, buyer_is_maker))
            st.last_ts, st.last_price = max(st.last_ts, ts), price
            if st.bin_size is None:
                st.bin_size = _two_sig(price * 0.0004)
            minute = ts - ts % BUCKET_MS
            b = st.buckets.get(minute)
            if b is None:
                b = st.buckets[minute] = Bucket(minute)
                for old in [m for m in st.buckets if m < minute - BUCKET_KEEP * BUCKET_MS]:
                    del st.buckets[old]
            b.n += 1
            lvl = b.levels.setdefault(int(price // st.bin_size), [0.0, 0.0])
            if aggressor_buy:
                b.buy_qty += qty; b.buy_usdt += usdt; lvl[0] += qty
            else:
                b.sell_qty += qty; b.sell_usdt += usdt; lvl[1] += qty
            st.notionals.append(usdt)
            st.since_recalc += 1
            if st.since_recalc >= 200 and len(st.notionals) >= 500:
                st.since_recalc = 0
                ordered = sorted(st.notionals)
                q995 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.995))]
                st.threshold = max(st.floor, q995)
            if usdt >= st.threshold:
                st.whales.append((ts, usdt, price, aggressor_buy))

    # ---- queries -------------------------------------------------------
    def last_ts(self, symbol: str) -> int:
        with self._lock:
            st = self._s.get(symbol)
            return st.last_ts if st else 0

    def last_price(self, symbol: str) -> Optional[float]:
        with self._lock:
            st = self._s.get(symbol)
            return st.last_price if st and st.last_price else None

    def threshold(self, symbol: str) -> float:
        with self._lock:
            return self._sym(symbol).threshold

    def trade_count(self, symbol: str) -> int:
        with self._lock:
            st = self._s.get(symbol)
            return len(st.trades) if st else 0

    def coverage_minutes(self, symbol: str) -> float:
        """How many minutes of trade history are buffered (warm-up indicator)."""
        with self._lock:
            st = self._s.get(symbol)
            if not st or not st.buckets:
                return 0.0
            return (max(st.buckets) - min(st.buckets)) / 60_000 + 1

    def buckets(self, symbol: str, minutes: int, now: Optional[int] = None) -> List[Bucket]:
        now = now if now is not None else now_ms()
        start = (now - now % BUCKET_MS) - (minutes - 1) * BUCKET_MS
        with self._lock:
            st = self._s.get(symbol)
            if not st:
                return []
            return [st.buckets[m] for m in sorted(st.buckets) if m >= start]

    def whale_stats(self, symbol: str, window_ms: int, now: Optional[int] = None) -> Dict[str, float]:
        now = now if now is not None else now_ms()
        buy = sell = largest = 0.0
        nb = ns = 0
        with self._lock:
            st = self._s.get(symbol)
            events = [w for w in (st.whales if st else []) if now - w[0] <= window_ms]
            thr = st.threshold if st else self.base_floor
        for _, usdt, _, agg_buy in events:
            largest = max(largest, usdt)
            if agg_buy:
                buy += usdt; nb += 1
            else:
                sell += usdt; ns += 1
        tot = buy + sell
        return {
            "buy_usdt": buy, "sell_usdt": sell, "net_usdt": buy - sell, "buy_count": nb, "sell_count": ns,
            "largest_usdt": largest, "ratio": (buy - sell) / tot if tot > 0 else 0.0, "threshold_usdt": thr,
        }

    def flow_profile(self, symbol: str, minutes: int = 5, now: Optional[int] = None, limit: int = 40) -> Dict[str, object]:
        """Return an aggTrade-derived volume/delta profile for the requested minute window.

        Binance aggTrade ``m`` semantics are applied in add(): buyer-is-maker means
        aggressive selling. The result is unavailable when no trade buckets exist.
        ``cvd`` is the signed base-asset quantity summed over the window (not a
        lifetime accumulator), so consumers can compare it consistently by window.
        """
        bks = self.buckets(symbol, minutes, now)
        levels: Dict[int, List[float]] = {}
        buy_qty = sell_qty = buy_usdt = sell_usdt = 0.0
        for bucket in bks:
            buy_qty += bucket.buy_qty
            sell_qty += bucket.sell_qty
            buy_usdt += bucket.buy_usdt
            sell_usdt += bucket.sell_usdt
            for price_bin, (bq, sq) in bucket.levels.items():
                row = levels.setdefault(price_bin, [0.0, 0.0])
                row[0] += bq
                row[1] += sq
        volume = buy_qty + sell_qty
        if not levels or volume <= 0:
            return {"available": False, "minutes": minutes, "buckets": len(bks)}
        poc = max(levels, key=lambda k: sum(levels[k]))
        rows = [{"bin": k, "buy_qty": round(v[0], 8), "sell_qty": round(v[1], 8),
                 "total_qty": round(v[0] + v[1], 8),
                 "delta_qty": round(v[0] - v[1], 8)} for k, v in sorted(levels.items())]
        rows.sort(key=lambda x: x["total_qty"], reverse=True)
        delta = buy_qty - sell_qty
        return {"available": True, "minutes": minutes, "buckets": len(bks),
                "buy_qty": round(buy_qty, 8), "sell_qty": round(sell_qty, 8),
                "buy_usdt": round(buy_usdt, 2), "sell_usdt": round(sell_usdt, 2),
                "volume_qty": round(volume, 8), "cvd": round(delta, 8),
                "delta_ratio": round(delta / volume, 6), "poc_bin": int(poc),
                "levels": rows[:max(1, min(int(limit), 200))]}

    def stacked_imbalances(self, symbol: str, minutes: int = 5, ratio: float = 3.0, min_qty_frac: float = 0.02,
                           now: Optional[int] = None) -> Dict[str, int]:
        """Footprint-style stacked diagonal imbalances over the last N minutes."""
        bks = self.buckets(symbol, minutes, now)
        merged: Dict[int, List[float]] = {}
        for b in bks:
            for k, (bq, sq) in b.levels.items():
                m = merged.setdefault(k, [0.0, 0.0])
                m[0] += bq; m[1] += sq
        if not merged:
            return {"buy_stacks": 0, "sell_stacks": 0, "levels": 0}
        total = sum(a + b for a, b in merged.values()) or 1.0
        floor = total * min_qty_frac
        flags = {}
        for k, (bq, sq) in merged.items():
            if bq + sq < floor:
                flags[k] = 0
            elif bq >= ratio * max(sq, 1e-12):
                flags[k] = 1
            elif sq >= ratio * max(bq, 1e-12):
                flags[k] = -1
            else:
                flags[k] = 0
        buy_stacks = sell_stacks = 0
        run, side, prev = 0, 0, None
        for k in sorted(flags):
            f = flags[k]
            if f != 0 and f == side and prev is not None and k - prev == 1:
                run += 1
            else:
                run, side = (1, f) if f != 0 else (0, 0)
            prev = k
            if run == 3:
                if side == 1:
                    buy_stacks += 1
                else:
                    sell_stacks += 1
        return {"buy_stacks": buy_stacks, "sell_stacks": sell_stacks, "levels": len(merged)}


class BookStore:
    """Latest partial-depth snapshot per symbol (top levels)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._b: Dict[str, dict] = {}

    def update(self, symbol: str, bids: list, asks: list, ts: Optional[int] = None) -> None:
        try:
            b = [(float(p), float(q)) for p, q in bids if float(q) > 0]
            a = [(float(p), float(q)) for p, q in asks if float(q) > 0]
        except (TypeError, ValueError):
            return
        if not b or not a:
            return
        with self._lock:
            self._b[symbol] = {"bids": b, "asks": a, "ts": ts or now_ms(), "rx": now_ms()}

    def get(self, symbol: str) -> Optional[dict]:
        with self._lock:
            snap = self._b.get(symbol)
            return dict(snap) if snap else None

    def age_ms(self, symbol: str, now: Optional[int] = None) -> Optional[int]:
        snap = self.get(symbol)
        return None if not snap else (now if now is not None else now_ms()) - snap["rx"]

    def top(self, symbol: str) -> Optional[Tuple[float, float, float]]:
        """(best_bid, best_ask, spread_pct)."""
        snap = self.get(symbol)
        if not snap:
            return None
        bid, ask = snap["bids"][0][0], snap["asks"][0][0]
        if bid <= 0 or ask < bid:
            return None
        mid = (bid + ask) / 2
        return bid, ask, (ask - bid) / mid * 100.0
