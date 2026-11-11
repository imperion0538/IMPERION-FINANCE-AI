"""Portfolio inventory and correlated-exposure guard for V16.1."""
from __future__ import annotations
from typing import Dict, List, Tuple

MAJORS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"})

class InventoryManager:
    """Read current runtime exposure and calculate a conservative size multiplier.

    Exposure rows are sourced from Lifecycle/Database, not an in-memory shadow list.
    Correlated majors are treated as one directional risk cluster.
    """
    def __init__(self, max_positions: int = 1, min_multiplier: float = 0.20):
        self.max_positions = max(1, int(max_positions))
        self.min_multiplier = max(0.0, min(1.0, float(min_multiplier)))

    @staticmethod
    def _notional(row: dict) -> float:
        qty = abs(float(row.get("qty") or 0.0))
        entry = abs(float(row.get("entry") or 0.0))
        return qty * entry

    def snapshot(self, positions: List[dict]) -> dict:
        long_n = short_n = 0.0
        by_symbol: Dict[str, float] = {}
        for row in positions:
            side = str(row.get("side") or "").upper()
            if side not in ("LONG", "SHORT"):
                continue
            n = self._notional(row)
            signed = n if side == "LONG" else -n
            sym = str(row.get("symbol") or "").upper()
            by_symbol[sym] = by_symbol.get(sym, 0.0) + signed
            if side == "LONG": long_n += n
            else: short_n += n
        total = long_n + short_n
        return {"positions": len(positions), "long_notional": round(long_n, 4),
                "short_notional": round(short_n, 4),
                "net_bias": round((long_n-short_n)/total, 4) if total else 0.0,
                "symbols": by_symbol,
                "major_long": round(sum(v for s,v in by_symbol.items() if s in MAJORS and v > 0), 4),
                "major_short": round(sum(-v for s,v in by_symbol.items() if s in MAJORS and v < 0), 4)}

    def assess(self, positions: List[dict], side: str, symbol: str) -> dict:
        snap = self.snapshot(positions)
        side = side.upper(); symbol = symbol.upper()
        mult = 1.0
        bias = snap["net_bias"]
        if side == "LONG" and bias > 0.35:
            mult *= max(0.35, 1.0 - (bias - 0.35) * 1.0)
        elif side == "SHORT" and bias < -0.35:
            mult *= max(0.35, 1.0 - (-bias - 0.35) * 1.0)
        # Same-symbol re-entry is vetoed by RiskManager.precheck (which has the
        # cooldown and pending-order context); inventory only scales portfolio concentration.
        if symbol in MAJORS:
            cluster = snap["major_long"] if side == "LONG" else snap["major_short"]
            if cluster > 0:
                mult *= 0.75
        if snap["positions"] > self.max_positions:
            mult = 0.0
        mult = round(max(0.0, min(1.0, mult)), 4)
        return {"allowed": mult >= self.min_multiplier, "size_multiplier": mult,
                "snapshot": snap, "reason": "" if mult >= self.min_multiplier else f"INVENTORY_LIMIT (size_multiplier={mult:.2f})"}
