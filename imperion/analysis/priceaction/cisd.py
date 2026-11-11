"""Deep Change in State of Delivery (CISD).

A high-quality CISD requires:
- Strong displacement candle that closes beyond a prior micro-range or structure level
- Preferably after a sweep or session liquidity take
- Follow-through on subsequent candles
- Clean body (high body-to-range ratio)
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from ...models import BEARISH, BULLISH, Candle
from .types import CISD, Sweep


def find_cisds(
    cs: Sequence[Candle],
    atrs: Sequence[float],
    sweeps: List[Sweep],
    atr: float,
    structure_protected: Optional[float] = None,
) -> List[CISD]:
    """Detect high-quality CISD candles with follow-through scoring."""
    if len(cs) < 25 or len(atrs) != len(cs) or atr <= 0:
        return []
    n = len(cs)
    out: List[CISD] = []

    for i in range(max(8, n - 15), n):
        c = cs[i]
        a = atrs[i] if atrs[i] > 0 else atr
        disp = c.body / a
        body_ratio = c.body / max(c.range, 1e-12)
        if disp < 1.35 or body_ratio < 0.62:
            continue
        direction = BULLISH if c.bullish else BEARISH

        # Micro range of previous 5-7 candles
        recent = cs[max(0, i - 7):i]
        if len(recent) < 3:
            continue
        r_hi = max(x.h for x in recent)
        r_lo = min(x.l for x in recent)
        closes_beyond = (direction == BULLISH and c.c > r_hi) or (direction == BEARISH and c.c < r_lo)
        if not closes_beyond:
            continue

        # Optional structure break (closes beyond protected or last swing extreme)
        closes_beyond_struct = False
        if structure_protected is not None:
            if direction == BULLISH and c.c > structure_protected:
                closes_beyond_struct = True
            elif direction == BEARISH and c.c < structure_protected:
                closes_beyond_struct = True

        age = n - 1 - i
        # Follow-through: next 1-3 candles continue in direction
        ft = 0.0
        nexts = cs[i + 1: i + 4]
        if nexts:
            if direction == BULLISH:
                continued = sum(1 for x in nexts if x.c > c.c or x.l > c.l)
                ft = continued / len(nexts)
            else:
                continued = sum(1 for x in nexts if x.c < c.c or x.h < c.h)
                ft = continued / len(nexts)

        # Base quality
        q = 0.50
        q += min(0.22, (disp - 1.35) * 0.10)
        q += 0.12 if body_ratio >= 0.70 else 0.05
        q += 0.10 * ft
        if closes_beyond_struct:
            q += 0.08

        after_sweep = any(0 <= i - s.index <= 5 and s.direction == direction for s in sweeps)
        if after_sweep:
            q += 0.10

        q = min(0.94, q)
        if q < 0.64:
            continue

        out.append(CISD(
            direction=direction,
            index=i,
            age=age,
            level=c.c,
            displacement=round(disp, 2),
            quality=round(q, 3),
            after_sweep=after_sweep,
            follow_through=round(ft, 2),
            closes_beyond_structure=closes_beyond_struct,
        ))

    out.sort(key=lambda x: (-x.quality, -x.index))
    return out[:3]
