"""Radar candidate selection and derivatives context.

Candidate presence never grants order permission. The runtime's optional
admission layer separately authorizes operator-allowlisted candidates.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Optional

from ..config import DYNAMIC_SCAN_LIMIT
from ..util import fnum, now_ms

log = logging.getLogger("imperion.universe")
_EXCLUDE_SUFFIX = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def pick_radar(tickers: List[Dict[str, Any]], tradable: Optional[Iterable[str]], fixed: Iterable[str],
               n: int = DYNAMIC_SCAN_LIMIT) -> List[Dict[str, Any]]:
    """Top-n by 24h quote volume among tradable USDT perpetuals that are not in the execution universe."""
    if not 0 <= int(n) <= DYNAMIC_SCAN_LIMIT:
        raise ValueError(f"radar size must be 0..{DYNAMIC_SCAN_LIMIT}")
    fixed_set = {s.upper() for s in fixed}
    allowed = {s.upper() for s in tradable} if tradable is not None else None
    rows: Dict[str, Dict[str, Any]] = {}
    for t in tickers:
        sym = str(t.get("symbol", "")).upper()
        if (not sym.endswith("USDT") or sym in fixed_set or sym.endswith(_EXCLUDE_SUFFIX) or sym in rows
                or (allowed is not None and sym not in allowed)):
            continue
        qv = fnum(t.get("quoteVolume"), 0.0) or 0.0
        if qv > 0:
            rows[sym] = {"symbol": sym, "quote_volume": qv, "price_change_pct": fnum(t.get("priceChangePercent"), 0.0), "role": "SCAN_ONLY"}
    return sorted(rows.values(), key=lambda r: (-r["quote_volume"], r["symbol"]))[:n]


class Radar:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.markets: List[Dict[str, Any]] = []
        self.updated_ms = 0

    def refresh(self, rest, filters: Dict[str, Any], fixed: Iterable[str], n: int) -> List[str]:
        tradable = [s for s, f in filters.items() if f.status == "TRADING" and f.contract_type == "PERPETUAL"] if filters else None
        picked = pick_radar(rest.ticker_24h(), tradable, fixed, n)
        with self._lock:
            self.markets, self.updated_ms = picked, now_ms()
        return [m["symbol"] for m in picked]

    def symbols(self) -> List[str]:
        with self._lock:
            return [m["symbol"] for m in self.markets]

    def candidates(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(m) for m in self.markets]


class DerivativesStore:
    """Funding / mark context from premiumIndex (one weight-1 call for all symbols)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data: Dict[str, Dict[str, float]] = {}
        self.updated_ms = 0

    def refresh(self, rest) -> int:
        out: Dict[str, Dict[str, float]] = {}
        for r in rest.premium_index():
            sym = str(r.get("symbol", "")).upper()
            rate = fnum(r.get("lastFundingRate"))
            if sym and rate is not None:
                out[sym] = {"funding": rate, "mark": fnum(r.get("markPrice"), 0.0) or 0.0,
                            "next_funding_ms": int(fnum(r.get("nextFundingTime"), 0) or 0)}
        with self._lock:
            self.data, self.updated_ms = out, now_ms()
        return len(out)

    def get(self, symbol: str) -> Optional[Dict[str, float]]:
        with self._lock:
            v = self.data.get(symbol.upper())
            return dict(v) if v else None

    def age_ms(self) -> Optional[int]:
        return now_ms() - self.updated_ms if self.updated_ms else None
