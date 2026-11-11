"""Data-health gate. Fail-closed: any missing/stale input blocks signal authorization."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from ..util import last_closed_open, now_ms, tf_ms
from .candles import DEFAULT_MIN_HISTORY, MIN_HISTORY, CandleStore
from .microstructure import BookStore, TradeStore

TRADE_MAX_AGE_MS = 180_000
BOOK_MAX_AGE_MS = 30_000
CANDLE_GRACE_MS = 15_000


@dataclass
class HealthReport:
    passed: bool
    checks: Dict[str, bool] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    quality: float = 0.0

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "reasons": self.reasons, "quality": round(self.quality, 3)}


class HealthMonitor:
    def __init__(self, candles: CandleStore, trades: TradeStore, books: BookStore, streams=None,
                 max_spread_pct: float = 0.06, require_streams: bool = True):
        self.candles, self.trades, self.books = candles, trades, books
        self.streams = streams
        self.max_spread_pct = max_spread_pct
        self.require_streams = require_streams

    def candles_fresh(self, symbol: str, tf: str, now: int) -> bool:
        have = self.candles.last_open(symbol, tf)
        if have is None:
            return False
        expected = last_closed_open(now, tf)
        if have >= expected:
            return True
        # Just after a boundary the new candle may not be published yet.
        return have == expected - tf_ms(tf) and now - (expected + tf_ms(tf)) < CANDLE_GRACE_MS

    def symbol_health(self, symbol: str, tfs: Iterable[str], now: Optional[int] = None) -> HealthReport:
        now = now if now is not None else now_ms()
        checks: Dict[str, bool] = {}
        reasons: List[str] = []
        for tf in tfs:
            need = MIN_HISTORY.get(tf, DEFAULT_MIN_HISTORY)
            checks[f"history_{tf}"] = self.candles.count(symbol, tf) >= need
            checks[f"fresh_{tf}"] = self.candles_fresh(symbol, tf, now)
            checks[f"gapfree_{tf}"] = not self.candles.has_gap(symbol, tf, 40)
        if self.require_streams:
            ws_ok = bool(self.streams and self.streams.available and self.streams.healthy())
            checks["ws_connected"] = ws_ok
            last = self.trades.last_ts(symbol)
            checks["trades_fresh"] = last > 0 and now - last <= TRADE_MAX_AGE_MS
            age = self.books.age_ms(symbol, now)
            checks["book_fresh"] = age is not None and age <= BOOK_MAX_AGE_MS
            top = self.books.top(symbol)
            checks["spread_ok"] = top is not None and top[2] <= self.max_spread_pct
        for key, ok in checks.items():
            if not ok:
                reasons.append(f"HEALTH_{key.upper()}_FAILED")
        quality = sum(checks.values()) / len(checks) if checks else 0.0
        return HealthReport(passed=all(checks.values()) and bool(checks), checks=checks, reasons=reasons, quality=quality)


def data_quality_gate(*, health_passed: bool, completed_candles: bool = True, latency_ms: Optional[float] = None,
                      max_latency_ms: float = 8000.0, spread_ok: bool = True) -> Dict[str, object]:
    """Last data check right before an entry: candles closed, trade stream fresh, spread acceptable."""
    reasons: List[str] = []
    if not health_passed:
        reasons.append("DATA_UNHEALTHY")
    if not completed_candles:
        reasons.append("CANDLE_NOT_CLOSED")
    if latency_ms is not None and latency_ms > max_latency_ms:
        reasons.append(f"LATENCY_TOO_HIGH ({latency_ms:.0f}ms)")
    if not spread_ok:
        reasons.append("SPREAD_TOO_WIDE")
    return {"passed": not reasons, "reasons": reasons, "latency_ms": latency_ms, "max_latency_ms": max_latency_ms}
