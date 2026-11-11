"""Isolated-margin liquidation math with the exchange's own maintenance-margin brackets.

Binance USDⓈ-M, one-way mode, one isolated position:
    LP = (WB + cum - side * Q * EP) / (Q * MMR - side * Q)        side = +1 long, -1 short
where WB is the isolated wallet balance (initial margin = Q * EP / leverage), MMR the maintenance-margin ratio and cum
the maintenance amount of the notional bracket the position falls into (GET /fapi/v1/leverageBracket).
Fees and funding move the real price slightly; the guardian therefore also compares the stop with the
liquidationPrice the exchange reports for the live position.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Used only when no exchange data exists (dry mode / simulator): deliberately conservative.
DEFAULT_BRACKETS: List[Dict[str, float]] = [
    {"bracket": 1, "initialLeverage": 20, "notionalFloor": 0.0, "notionalCap": 1e18, "maintMarginRatio": 0.02, "cum": 0.0},
]


def parse_brackets(data: Any, symbol: str) -> List[Dict[str, float]]:
    """Normalise a leverageBracket response (list of {symbol, brackets} or a single object) for `symbol`."""
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if isinstance(row, dict) and str(row.get("symbol", symbol)).upper() == symbol.upper() and isinstance(row.get("brackets"), list):
            out = []
            for b in row["brackets"]:
                out.append({"bracket": int(b.get("bracket", 0)), "initialLeverage": float(b["initialLeverage"]),
                            "notionalFloor": float(b.get("notionalFloor", 0)), "notionalCap": float(b["notionalCap"]),
                            "maintMarginRatio": float(b["maintMarginRatio"]), "cum": float(b.get("cum", 0))})
            if out:
                return sorted(out, key=lambda x: x["notionalFloor"])
    raise ValueError(f"no leverage brackets for {symbol} in exchange response")


def bracket_for(brackets: Sequence[Dict[str, float]], notional: float) -> Dict[str, float]:
    for b in brackets:
        if b["notionalFloor"] <= notional < b["notionalCap"]:
            return b
    return brackets[-1]


def isolated_liquidation_price(side: str, entry: float, qty: float, leverage: float, mmr: float, cum: float = 0.0) -> float:
    if qty <= 0 or entry <= 0 or leverage <= 0:
        raise ValueError("qty, entry and leverage must be positive")
    s = 1.0 if side == "LONG" else -1.0
    wb = qty * entry / leverage
    lp = (wb + cum - s * qty * entry) / (qty * mmr - s * qty)
    return max(0.0, lp)


def assess(side: str, entry: float, stop: float, qty: float, leverage: int, brackets: Optional[Sequence[Dict[str, float]]],
           safety: float) -> Dict[str, Any]:
    """Liquidation price of the planned position and whether the stop sits safely before it.

    `safety` = largest allowed (stop distance / liquidation distance); 0.7 means the stop must trigger within the first
    70 % of the way to liquidation.
    """
    source = "exchange" if brackets else "default"
    br = bracket_for(brackets or DEFAULT_BRACKETS, qty * entry)
    liq = isolated_liquidation_price(side, entry, qty, leverage, br["maintMarginRatio"], br["cum"])
    liq_dist = abs(entry - liq)
    stop_dist = abs(entry - stop)
    beyond = (side == "LONG" and stop <= liq) or (side == "SHORT" and stop >= liq)
    reasons = []
    if leverage > br["initialLeverage"]:
        reasons.append(f"LEVERAGE_ABOVE_BRACKET ({leverage}x > {br['initialLeverage']:.0f}x allowed at this size)")
    if beyond or liq_dist <= 0 or stop_dist > safety * liq_dist:
        reasons.append(f"STOP_TOO_CLOSE_TO_LIQUIDATION (stop {stop_dist / entry * 100:.2f}% away, liquidation {liq:.6g} "
                       f"{liq_dist / entry * 100:.2f}% away, mmr {br['maintMarginRatio'] * 100:.2f}% {source})")
    return {"ok": not reasons, "reasons": reasons, "liq_price": liq, "mmr": br["maintMarginRatio"], "cum": br["cum"],
            "max_leverage": br["initialLeverage"], "source": source,
            "stop_to_liq": round(stop_dist / liq_dist, 4) if liq_dist > 0 else None}


def stop_beyond_liquidation(side: str, stop: float, liq_price: float, margin: float = 0.0) -> bool:
    """True when the exchange-reported liquidation would happen before (or at) the stop."""
    if not liq_price or liq_price <= 0:
        return False
    if side == "LONG":
        return stop <= liq_price * (1 + margin)
    return stop >= liq_price * (1 - margin)
