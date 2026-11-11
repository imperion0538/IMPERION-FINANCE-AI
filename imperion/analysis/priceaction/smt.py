"""Confirmed-swing SMT divergence for correlated instrument pairs.

This is a research/confirmation feature: it never submits orders and it only
compares confirmed fractal swings (right-side candles have already closed).
The caller must choose economically related symbols and matching timeframes.
"""
from __future__ import annotations

from typing import Dict, Sequence

from ...models import Candle
from .structure import find_swings


def smt_divergence(primary: Sequence[Candle], comparison: Sequence[Candle], *,
                   lookback: int = 30, left: int = 3, right: int = 3) -> Dict[str, object]:
    """Detect bearish (primary HH, comparison not HH) or bullish (primary LL, comparison not LL) SMT.

    Both inputs must be same-timeframe, chronologically ordered candle series.
    Returns unavailable rather than guessing when the series are short or their
    latest timestamps are not aligned within one candle interval.
    """
    if len(primary) < left + right + 8 or len(comparison) < left + right + 8:
        return {"available": False, "reason": "insufficient_history"}
    pstep = primary[-1].t - primary[-2].t
    cstep = comparison[-1].t - comparison[-2].t
    if pstep <= 0 or cstep <= 0 or abs(primary[-1].t - comparison[-1].t) > max(pstep, cstep):
        return {"available": False, "reason": "unaligned_series"}

    def recent(kind: str, series: Sequence[Candle]):
        swings = [x for x in find_swings(series, left, right)
                  if x.kind == kind and len(series) - 1 - x.i <= lookback + right]
        return swings[-2:]

    ph, ch = recent("H", primary), recent("H", comparison)
    pl, cl = recent("L", primary), recent("L", comparison)
    signals = []
    if len(ph) == len(ch) == 2:
        p_hh = ph[1].price > ph[0].price
        c_hh = ch[1].price > ch[0].price
        p_lh = ph[1].price < ph[0].price
        c_lh = ch[1].price < ch[0].price
        if p_hh and not c_hh:
            signals.append({"direction": "BEARISH", "type": "HIGH_MISMATCH", "primary": "HH",
                            "comparison": "HH" if c_hh else "LH_OR_EQUAL"})
        elif c_hh and not p_hh:
            signals.append({"direction": "BULLISH", "type": "HIGH_MISMATCH", "primary": "LH_OR_EQUAL",
                            "comparison": "HH"})
    if len(pl) == len(cl) == 2:
        p_ll = pl[1].price < pl[0].price
        c_ll = cl[1].price < cl[0].price
        if p_ll and not c_ll:
            signals.append({"direction": "BULLISH", "type": "LOW_MISMATCH", "primary": "LL",
                            "comparison": "LL" if c_ll else "HL_OR_EQUAL"})
        elif c_ll and not p_ll:
            signals.append({"direction": "BEARISH", "type": "LOW_MISMATCH", "primary": "HL_OR_EQUAL",
                            "comparison": "LL"})
    directions = {x["direction"] for x in signals}
    direction = next(iter(directions)) if len(directions) == 1 else "CONFLICT" if directions else "NONE"
    return {"available": True, "direction": direction, "signals": signals,
            "primary_last_ts": primary[-1].t, "comparison_last_ts": comparison[-1].t,
            "confirmed_only": True}
