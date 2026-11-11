"""Candle-level price action on the trigger timeframe: the confirmation a discretionary trader waits for."""
from __future__ import annotations

from typing import List, Sequence, Tuple

from ...models import BEARISH, BULLISH, NEUTRAL, Candle
from .types import CandleSignal


def candle_signal(cs: Sequence[Candle], atr: float) -> CandleSignal:
    """Strongest pattern of the last three candles. Opposite patterns cancel out (NEUTRAL)."""
    if len(cs) < 4 or atr <= 0:
        return CandleSignal()
    c1, c2, c3 = cs[-3], cs[-2], cs[-1]
    found: List[Tuple[str, float, str]] = []
    rng = max(c3.range, 1e-12)
    # engulfing
    if c3.bullish and not c2.bullish and c3.c >= c2.o and c3.o <= c2.c and c3.body >= 0.6 * atr and c3.body > c2.body:
        found.append((BULLISH, 0.62, "bullish engulfing"))
    if (not c3.bullish) and c2.bullish and c3.c <= c2.o and c3.o >= c2.c and c3.body >= 0.6 * atr and c3.body > c2.body:
        found.append((BEARISH, 0.62, "bearish engulfing"))
    # rejection (pin bar)
    if rng >= 0.8 * atr:
        lower, upper = min(c3.o, c3.c) - c3.l, c3.h - max(c3.o, c3.c)
        if lower >= 2 * max(c3.body, 1e-12) and lower >= 0.55 * rng and (c3.c - c3.l) / rng >= 0.6:
            found.append((BULLISH, 0.56, "bullish rejection wick"))
        if upper >= 2 * max(c3.body, 1e-12) and upper >= 0.55 * rng and (c3.h - c3.c) / rng >= 0.6:
            found.append((BEARISH, 0.56, "bearish rejection wick"))
    # displacement close
    if c3.body >= 1.3 * atr and c3.body >= 0.7 * rng:
        found.append((BULLISH if c3.bullish else BEARISH, 0.55, "displacement candle"))
    # inside-bar breakout
    if c2.h <= c1.h and c2.l >= c1.l:
        if c3.c > c1.h:
            found.append((BULLISH, 0.48, "inside-bar breakout"))
        elif c3.c < c1.l:
            found.append((BEARISH, 0.48, "inside-bar breakdown"))
    # three-candle formations
    if (not c1.bullish) and c1.body >= 0.8 * atr and c2.body <= 0.4 * c1.body and c3.bullish and c3.c > (c1.o + c1.c) / 2:
        found.append((BULLISH, 0.6, "morning star"))
    if c1.bullish and c1.body >= 0.8 * atr and c2.body <= 0.4 * c1.body and (not c3.bullish) and c3.c < (c1.o + c1.c) / 2:
        found.append((BEARISH, 0.6, "evening star"))
    if all(c.bullish and c.body >= 0.5 * atr for c in (c1, c2, c3)) and c1.c < c2.c < c3.c:
        found.append((BULLISH, 0.5, "three white soldiers"))
    if all((not c.bullish) and c.body >= 0.5 * atr for c in (c1, c2, c3)) and c1.c > c2.c > c3.c:
        found.append((BEARISH, 0.5, "three black crows"))
    if not found:
        return CandleSignal()
    if len({f[0] for f in found}) > 1:
        return CandleSignal(NEUTRAL, 0.0, [f[2] for f in found])
    direction = found[0][0]
    strength = min(0.85, max(f[1] for f in found) + 0.05 * (len(found) - 1))
    return CandleSignal(direction, strength, [f[2] for f in found])
