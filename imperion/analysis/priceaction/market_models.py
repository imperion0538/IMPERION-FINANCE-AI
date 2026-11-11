"""Research-grade accumulation/manipulation/distribution pattern candidates.

This detector emits explainable candidates only; it is not a standalone trade signal.
"""
from __future__ import annotations
from typing import Sequence, Dict, Any
from ...models import Candle


def detect_mmbm_candidate(candles: Sequence[Candle], atr: float, lookback: int = 36) -> Dict[str, Any]:
    if len(candles) < max(lookback + 5, 50) or atr <= 0:
        return {"available": False, "model": "MMBM_MMSM", "reason": "insufficient_history"}
    window = list(candles[-lookback-5:-5])
    recent = list(candles[-5:])
    hi, lo = max(x.h for x in window), min(x.l for x in window)
    width_atr = (hi - lo) / atr
    if width_atr > 5.0 or width_atr < 0.8:
        return {"available": True, "candidate": False, "reason": "no_compact_range", "range_atr": round(width_atr, 2)}
    last = recent[-1]
    swept_high = any(x.h > hi and x.c < hi for x in recent)
    swept_low = any(x.l < lo and x.c > lo for x in recent)
    bullish_disp = last.c > last.o and last.body / atr >= 1.25 and last.c > max(x.h for x in recent[:-1])
    bearish_disp = last.c < last.o and last.body / atr >= 1.25 and last.c < min(x.l for x in recent[:-1])
    direction = "BULLISH" if swept_low and bullish_disp else "BEARISH" if swept_high and bearish_disp else "NONE"
    return {"available": True, "candidate": direction != "NONE", "model": "MMBM_MMSM",
            "direction": direction, "range_atr": round(width_atr, 2),
            "accumulation_range": {"high": hi, "low": lo},
            "manipulation": "SELL_SIDE_SWEEP" if swept_low else "BUY_SIDE_SWEEP" if swept_high else "NONE",
            "distribution_confirmation": "BULLISH_DISPLACEMENT" if bullish_disp else "BEARISH_DISPLACEMENT" if bearish_disp else "NONE",
            "research_only": True}
