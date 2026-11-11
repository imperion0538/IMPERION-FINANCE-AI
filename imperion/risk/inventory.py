
"""IMPERION FINANCE AI V17 - Portfolio Inventory Guard.

Conservative inventory and correlated-exposure assessment.

This module does not authorize trades by itself.
A trade must also pass RiskManager, execution and live gates.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List


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

# Compatibility with existing V16.1 imports.
MAJORS = frozenset({
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
})

# Risk clusters are conservative approximations.
# They are not claims of fixed statistical correlations.
RISK_CLUSTERS = {
    "LARGE_CAP": frozenset({
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    }),
    "LAYER_1": frozenset({
        "AVAXUSDT",
        "SUIUSDT",
        "DOTUSDT",
        "NEOUSDT",
    }),
    "DEFI_INFRA": frozenset({
        "LINKUSDT",
        "INJUSDT",
    }),
    "LEGACY": frozenset({
        "XRPUSDT",
        "LTCUSDT",
        "DASHUSDT",
    }),
    "STORAGE": frozenset({
        "FILUSDT",
    }),
}


def _finite_nonnegative(value: Any) -> float:
    """Reject invalid, non-finite and negative numeric values."""
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid numeric value")

    number = float(value)

    if not math.isfinite(number) or number < 0:
        raise ValueError("Invalid nonnegative numeric value")

    return number


def _finite_positive(value: Any) -> float:
    number = _finite_nonnegative(value)

    if number <= 0:
        raise ValueError("Expected a positive numeric value")

    return number


class InventoryManager:
    """Conservative portfolio inventory risk guard.

    Public interface remains compatible with V16.1:
        snapshot(positions)
        assess(positions, side, symbol)

    Fail-closed policy:
        Invalid exposure data blocks new entries.
        Unsupported symbols block new entries.
        Position limits cannot be bypassed by sizing.
    """

    def __init__(
        self,
        max_positions: int = 1,
        min_multiplier: float = 0.20,
    ):
        if isinstance(max_positions, bool):
            raise ValueError("Invalid max_positions")

        self.max_positions = max(1, int(max_positions))

        minimum = _finite_nonnegative(min_multiplier)

        if minimum > 1:
            raise ValueError(
                "min_multiplier cannot exceed 1"
            )

        self.min_multiplier = minimum

    @staticmethod
    def _notional(row: dict) -> float:
        if not isinstance(row, dict):
            raise ValueError("Invalid position record")

        qty = _finite_positive(row.get("qty"))
        entry = _finite_positive(row.get("entry"))

        notional = qty * entry

        if not math.isfinite(notional) or notional <= 0:
            raise ValueError("Invalid position notional")

        return notional

    @staticmethod
    def _normalize_position(row: dict) -> dict:
        if not isinstance(row, dict):
            raise ValueError("Invalid position record")

        symbol = str(
            row.get("symbol") or ""
        ).strip().upper()

        side = str(
            row.get("side") or ""
        ).strip().upper()

        if not symbol:
            raise ValueError("Missing position symbol")

        if side not in ("LONG", "SHORT"):
            raise ValueError("Invalid position side")

        return {
            "symbol": symbol,
            "side": side,
            "notional": InventoryManager._notional(row),
        }

    @staticmethod
    def _cluster_names(symbol: str) -> List[str]:
        return [
            name
            for name, members in RISK_CLUSTERS.items()
            if symbol in members
        ]

    def snapshot(self, positions: List[dict]) -> dict:
        """Build a validated exposure snapshot.

        Raises ValueError when positions are malformed.
        The caller must treat such failure as a trade veto.
        """
        if not isinstance(positions, (list, tuple)):
            raise ValueError("Invalid positions collection")

        long_n = 0.0
        short_n = 0.0

        by_symbol: Dict[str, float] = {}
        cluster_long: Dict[str, float] = {
            name: 0.0 for name in RISK_CLUSTERS
        }
        cluster_short: Dict[str, float] = {
            name: 0.0 for name in RISK_CLUSTERS
        }

        normalized = []

        for raw in positions:
            row = self._normalize_position(raw)
            normalized.append(row)

            symbol = row["symbol"]
            side = row["side"]
            notional = row["notional"]

            signed = (
                notional if side == "LONG"
                else -notional
            )

            by_symbol[symbol] = (
                by_symbol.get(symbol, 0.0) + signed
            )

            if side == "LONG":
                long_n += notional
            else:
                short_n += notional

            for cluster in self._cluster_names(symbol):
                if side == "LONG":
                    cluster_long[cluster] += notional
                else:
                    cluster_short[cluster] += notional

        total = long_n + short_n

        if not math.isfinite(total):
            raise ValueError("Invalid total exposure")

        bias = (
            (long_n - short_n) / total
            if total > 0
            else 0.0
        )

        return {
            "positions": len(normalized),
            "long_notional": round(long_n, 4),
            "short_notional": round(short_n, 4),
            "net_bias": round(bias, 4),
            "symbols": by_symbol,
            "major_long": round(
                sum(
                    value
                    for symbol, value in by_symbol.items()
                    if symbol in MAJORS and value > 0
                ),
                4,
            ),
            "major_short": round(
                sum(
                    -value
                    for symbol, value in by_symbol.items()
                    if symbol in MAJORS and value < 0
                ),
                4,
            ),
            "cluster_long": {
                name: round(value, 4)
                for name, value in cluster_long.items()
            },
            "cluster_short": {
                name: round(value, 4)
                for name, value in cluster_short.items()
            },
        }

    def _blocked(
        self,
        reason: str,
        snapshot: dict | None = None,
    ) -> dict:
        return {
            "allowed": False,
            "size_multiplier": 0.0,
            "snapshot": snapshot or {},
            "reason": reason,
        }

    def assess(
        self,
        positions: List[dict],
        side: str,
        symbol: str,
    ) -> dict:
        """Evaluate a prospective new entry.

        This function never increases position size.
        A returned allowance is not final trade approval.
        """
        side = str(side or "").strip().upper()
        symbol = str(symbol or "").strip().upper()

        if side not in ("LONG", "SHORT"):
            return self._blocked(
                "INVENTORY_INVALID_SIDE"
            )

        if symbol not in FIXED_SYMBOLS:
            return self._blocked(
                "INVENTORY_SYMBOL_NOT_ALLOWED"
            )

        try:
            snap = self.snapshot(positions)
        except (ValueError, TypeError, OverflowError):
            return self._blocked(
                "INVENTORY_INVALID_EXPOSURE_DATA"
            )

        # Block entries once the position limit is reached.
        if snap["positions"] >= self.max_positions:
            return self._blocked(
                "INVENTORY_MAX_POSITIONS",
                snap,
            )

        # Same-symbol re-entry is blocked here as an
        # additional layer, regardless of other guards.
        if symbol in snap["symbols"]:
            return self._blocked(
                "INVENTORY_DUPLICATE_SYMBOL",
                snap,
            )

        multiplier = 1.0
        bias = snap["net_bias"]

        # Reduce risk when adding to an already
        # directionally concentrated portfolio.
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

        # Additional conservative haircut when the
        # same directional risk cluster is occupied.
        for cluster in self._cluster_names(symbol):
            exposure = (
                snap["cluster_long"][cluster]
                if side == "LONG"
                else snap["cluster_short"][cluster]
            )

            if exposure > 0:
                multiplier *= 0.75

        # Preserve the legacy major-coin haircut.
        if symbol in MAJORS:
            major_exposure = (
                snap["major_long"]
                if side == "LONG"
                else snap["major_short"]
            )

            if major_exposure > 0:
                multiplier *= 0.75

        multiplier = round(
            max(0.0, min(1.0, multiplier)),
            4,
        )

        allowed = (
            multiplier > 0
            and multiplier >= self.min_multiplier
        )

        return {
            "allowed": allowed,
            "size_multiplier": (
                multiplier if allowed else 0.0
            ),
            "snapshot": snap,
            "reason": (
                ""
                if allowed
                else (
                    "INVENTORY_LIMIT "
                    f"(size_multiplier={multiplier:.2f})"
                )
            ),
        }


# INVENTORY_PY_END
