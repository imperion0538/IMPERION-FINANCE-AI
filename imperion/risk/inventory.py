
"""IMPERION FINANCE AI V17 - Portfolio Inventory Guard.

Protects the portfolio against excessive position counts,
duplicate-symbol exposure, and correlated directional risk.

The execution universe is controlled by Settings and Radar,
not by a hard-coded whitelist in this module.

Inventory approval is never sufficient to authorize an order.
RiskManager, data-quality, execution and live gates must
independently approve every trade.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


FIXED_SYMBOLS = frozenset({
    "BTCUSDT",
    "ETHUSDT",
    "AVAXUSDT",
    "INJUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "FILUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "SUIUSDT",
    "LTCUSDT",
    "DASHUSDT",
    "NEOUSDT",
})

# Keep BNB for compatibility with the existing
# V16.1 correlated-major risk classification.
MAJORS = frozenset({
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
})

RISK_CLUSTERS = {
    "MAJORS": MAJORS,
    "LAYER_1": frozenset({
        "AVAXUSDT",
        "SUIUSDT",
        "DOTUSDT",
        "NEOUSDT",
    }),
    "INFRASTRUCTURE": frozenset({
        "LINKUSDT",
        "INJUSDT",
        "FILUSDT",
    }),
    "PAYMENTS_LEGACY": frozenset({
        "XRPUSDT",
        "LTCUSDT",
        "DASHUSDT",
    }),
}

MAX_EXPOSURE_MULTIPLIER = 1.0


def _number(value: Any) -> float:
    """Convert numeric input without accepting NaN or infinity."""
    if isinstance(value, bool):
        raise ValueError("Boolean numeric input")

    number = float(value)

    if not math.isfinite(number):
        raise ValueError("Non-finite numeric input")

    return number


def _positive(value: Any) -> float:
    number = _number(value)

    if number <= 0:
        raise ValueError("Expected positive numeric input")

    return number


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()

    if (
        not symbol
        or not symbol.isascii()
        or not symbol.isalnum()
        or not symbol.endswith("USDT")
    ):
        raise ValueError("Invalid USDT symbol")

    return symbol


class InventoryManager:
    """Portfolio exposure guard with a V16.1-compatible API.

    Public methods:
        snapshot(positions)
        assess(positions, side, symbol)

    Unknown symbols are not automatically authorized.
    The caller must enforce the configured execution universe.

    Invalid exposure quantities fail closed.
    """

    def __init__(
        self,
        max_positions: int = 1,
        min_multiplier: float = 0.20,
    ):
        if isinstance(max_positions, bool):
            raise ValueError("Invalid max_positions")

        count = int(max_positions)

        if count < 1:
            raise ValueError(
                "max_positions must be positive"
            )

        minimum = _number(min_multiplier)

        if not 0 <= minimum <= 1:
            raise ValueError(
                "min_multiplier must be between 0 and 1"
            )

        self.max_positions = count
        self.min_multiplier = minimum

    @staticmethod
    def _notional(row: dict) -> float:
        if not isinstance(row, dict):
            raise ValueError("Invalid position record")

        qty = abs(_number(row.get("qty")))
        entry = abs(_number(row.get("entry")))

        if qty <= 0 or entry <= 0:
            raise ValueError(
                "Invalid position quantity or entry"
            )

        notional = qty * entry

        if not math.isfinite(notional):
            raise ValueError(
                "Position notional overflow"
            )

        return notional

    @staticmethod
    def _clusters(symbol: str) -> List[str]:
        return [
            name
            for name, members in RISK_CLUSTERS.items()
            if symbol in members
        ]

    def snapshot(self, positions: List[dict]) -> dict:
        """Return validated portfolio exposure.

        An invalid position raises ValueError.
        The assess() method converts that into a trade veto.
        """
        if not isinstance(positions, (list, tuple)):
            raise ValueError(
                "Invalid positions collection"
            )

        long_n = 0.0
        short_n = 0.0
        by_symbol: Dict[str, float] = {}

        cluster_long = {
            name: 0.0 for name in RISK_CLUSTERS
        }
        cluster_short = {
            name: 0.0 for name in RISK_CLUSTERS
        }

        symbols_present = set()

        for row in positions:
            if not isinstance(row, dict):
                raise ValueError(
                    "Invalid position record"
                )

            symbol = _normalize_symbol(
                row.get("symbol")
            )
            side = str(
                row.get("side") or ""
            ).strip().upper()

            if side not in ("LONG", "SHORT"):
                raise ValueError(
                    "Invalid position side"
                )

            notional = self._notional(row)
            symbols_present.add(symbol)

            signed = (
                notional
                if side == "LONG"
                else -notional
            )

            by_symbol[symbol] = (
                by_symbol.get(symbol, 0.0)
                + signed
            )

            if side == "LONG":
                long_n += notional
            else:
                short_n += notional

            for cluster in self._clusters(symbol):
                if side == "LONG":
                    cluster_long[cluster] += notional
                else:
                    cluster_short[cluster] += notional

        total = long_n + short_n

        if not math.isfinite(total):
            raise ValueError(
                "Invalid portfolio exposure"
            )

        bias = (
            (long_n - short_n) / total
            if total > 0
            else 0.0
        )

        major_long = sum(
            value
            for symbol, value in by_symbol.items()
            if symbol in MAJORS and value > 0
        )

        major_short = sum(
            -value
            for symbol, value in by_symbol.items()
            if symbol in MAJORS and value < 0
        )

        return {
            "positions": len(positions),
            "long_notional": round(long_n, 4),
            "short_notional": round(short_n, 4),
            "net_bias": round(bias, 4),
            "symbols": by_symbol,
            "symbols_present": sorted(symbols_present),
            "major_long": round(major_long, 4),
            "major_short": round(major_short, 4),
            "cluster_long": {
                name: round(value, 4)
                for name, value in cluster_long.items()
            },
            "cluster_short": {
                name: round(value, 4)
                for name, value in cluster_short.items()
            },
        }

    @staticmethod
    def _blocked(
        reason: str,
        snapshot: Optional[dict] = None,
    ) -> dict:
        return {
            "allowed": False,
            "size_multiplier": 0.0,
            "snapshot": (
                snapshot
                if snapshot is not None
                else {}
            ),
            "reason": reason,
        }

    def assess(
        self,
        positions: List[dict],
        side: str,
        symbol: str,
    ) -> dict:
        """Assess a proposed new trade conservatively.

        This guard can reduce or reject position size.
        It never increases planned risk.
        """
        side = str(side or "").strip().upper()

        if side not in ("LONG", "SHORT"):
            return self._blocked(
                "INVENTORY_INVALID_SIDE"
            )

        try:
            symbol = _normalize_symbol(symbol)
            snap = self.snapshot(positions)
        except (ValueError, TypeError, OverflowError):
            return self._blocked(
                "INVENTORY_INVALID_EXPOSURE_DATA"
            )

        # Duplicate-symbol risk is checked before the
        # portfolio count so the reason remains explicit.
        if symbol in snap["symbols_present"]:
            return self._blocked(
                "SYMBOL_ALREADY_HAS_POSITION",
                snap,
            )

        # A new position cannot exceed the configured
        # concurrent-position limit.
        if snap["positions"] >= self.max_positions:
            return self._blocked(
                "INVENTORY_LIMIT "
                "(MAX_CONCURRENT_POSITIONS)",
                snap,
            )

        multiplier = 1.0
        bias = snap["net_bias"]

        # Portfolio directional concentration.
        if side == "LONG" and bias > 0.35:
            multiplier *= max(
                0.35,
                1.0 - (bias - 0.35),
            )

        elif side == "SHORT" and bias < -0.35:
            multiplier *= max(
                0.35,
                1.0 - (-bias - 0.35),
            )

        # Correlated-cluster exposure haircut.
        for cluster in self._clusters(symbol):
            exposure = (
                snap["cluster_long"][cluster]
                if side == "LONG"
                else snap["cluster_short"][cluster]
            )

            if exposure > 0:
                multiplier *= 0.75

        multiplier = round(
            max(
                0.0,
                min(
                    MAX_EXPOSURE_MULTIPLIER,
                    multiplier,
                ),
            ),
            4,
        )

        if (
            multiplier <= 0
            or multiplier < self.min_multiplier
        ):
            return self._blocked(
                "INVENTORY_LIMIT "
                f"(size_multiplier={multiplier:.2f})",
                snap,
            )

        return {
            "allowed": True,
            "size_multiplier": multiplier,
            "snapshot": snap,
            "reason": "",
        }


# INVENTORY_PY_END
