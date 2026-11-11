"""Swings, market structure (BOS / CHoCH state machine) and the dealing range.

Structure is read the way a discretionary trader reads it: a trend exists while price keeps CLOSING through the
swings in its direction (break of structure) and ends with the first close through the swing that launched the
last break (change of character). Two changes of character in a row mean the market is flipping, i.e. ranging.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from ...models import Candle
from .types import Break, DealingRange, Structure, Swing

SWING_LEFT = SWING_RIGHT = 3
MINOR_LEFT = MINOR_RIGHT = 2
CHOP_WINDOW = 40        # two consecutive CHoCHs inside this many candles = range
STALE_BREAK = 90        # a break older than this no longer defines a trend
MAJOR_LOOKBACK = 40
RANGE_WINDOW = 60
MIN_RANGE_ATR = 1.5


def find_swings(cs: Sequence[Candle], left: int = SWING_LEFT, right: int = SWING_RIGHT) -> List[Swing]:
    """Confirmed fractal swings: strictly beyond the `left` candles before, not exceeded by the `right` candles after."""
    out: List[Swing] = []
    for i in range(left, len(cs) - right):
        hi, lo = cs[i].h, cs[i].l
        if all(hi > cs[j].h for j in range(i - left, i)) and all(hi >= cs[j].h for j in range(i + 1, i + right + 1)):
            out.append(Swing(i, cs[i].t, hi, "H"))
        if all(lo < cs[j].l for j in range(i - left, i)) and all(lo <= cs[j].l for j in range(i + 1, i + right + 1)):
            out.append(Swing(i, cs[i].t, lo, "L"))
    return out


def analyze_structure(cs: Sequence[Candle], swings: Sequence[Swing], atrs: Sequence[float], right: int = SWING_RIGHT) -> Structure:
    n = len(cs)
    order = sorted(swings, key=lambda s: (s.i, s.kind))
    highs = [s for s in order if s.kind == "H"]
    lows = [s for s in order if s.kind == "L"]
    st = Structure()
    trend = "UNKNOWN"
    prev_high = prev_low = None
    k = 0
    for i in range(n):
        while k < len(order) and order[k].i + right <= i:       # a swing only exists once it is confirmed
            s = order[k]
            k += 1
            if s.kind == "H":
                st.ref_high, prev_high, st.last_high = s, st.last_high, s
            else:
                st.ref_low, prev_low, st.last_low = s, st.last_low, s
        c = cs[i]
        a = atrs[i] if i < len(atrs) and atrs[i] > 0 else max(c.range, 1e-12)
        if st.ref_high is not None and c.c > st.ref_high.price:
            ref = st.ref_high
            origin = min(range(ref.i, i + 1), key=lambda j: cs[j].l)
            major = all(ref.price >= s.price for s in highs if i - MAJOR_LOOKBACK <= s.i <= i)
            st.breaks.append(Break("CHOCH" if trend == "DOWN" else "BOS", "UP", ref.price, i, c.body / a, major))
            trend, st.protected, st.protected_i, st.ref_high = "UP", cs[origin].l, origin, None
        elif st.ref_low is not None and c.c < st.ref_low.price:
            ref = st.ref_low
            origin = max(range(ref.i, i + 1), key=lambda j: cs[j].h)
            major = all(ref.price <= s.price for s in lows if i - MAJOR_LOOKBACK <= s.i <= i)
            st.breaks.append(Break("CHOCH" if trend == "UP" else "BOS", "DOWN", ref.price, i, c.body / a, major))
            trend, st.protected, st.protected_i, st.ref_low = "DOWN", cs[origin].h, origin, None
    for b in st.breaks:
        b.age = n - 1 - b.index
    st.breaks = st.breaks[-8:]
    st.last_break = st.breaks[-1] if st.breaks else None
    if st.last_break is None:
        st.trend = "UNKNOWN"
    else:
        flipping = (len(st.breaks) >= 2 and st.breaks[-1].kind == "CHOCH" and st.breaks[-2].kind == "CHOCH"
                    and st.breaks[-2].age <= CHOP_WINDOW)
        st.trend = "RANGE" if flipping or st.last_break.age > STALE_BREAK else trend
    if st.last_high and prev_high and st.last_low and prev_low:
        hh, hl = st.last_high.price > prev_high.price, st.last_low.price > prev_low.price
        lh, ll = st.last_high.price < prev_high.price, st.last_low.price < prev_low.price
        st.swing_bias = "HH_HL" if hh and hl else "LH_LL" if lh and ll else "MIXED"
    return st


def dealing_range(cs: Sequence[Candle], st: Structure, atr: float) -> Optional[DealingRange]:
    """The range price is currently dealing in: the impulse leg of a live trend, otherwise the recent window.

    Also computes precise Fibonacci-based OTE zones relative to the impulse leg.
    """
    if not cs:
        return None
    price = cs[-1].c
    hi = lo = None
    source = "window"
    leg_start = None
    if st.trend == "UP" and st.protected_i is not None:
        lo, hi, source = st.protected, max(c.h for c in cs[st.protected_i:]), "leg"
        leg_start = st.protected_i
    elif st.trend == "DOWN" and st.protected_i is not None:
        hi, lo, source = st.protected, min(c.l for c in cs[st.protected_i:]), "leg"
        leg_start = st.protected_i
    if hi is None or lo is None or hi - lo < MIN_RANGE_ATR * atr:
        window = cs[-RANGE_WINDOW:]
        hi, lo, source = max(c.h for c in window), min(c.l for c in window), "window"
        leg_start = max(0, len(cs) - RANGE_WINDOW)
    if hi <= lo:
        return None

    position = (price - lo) / (hi - lo)
    # Fibonacci OTE on the impulse leg (from protected / start to extreme)
    # Classic OTE: 0.618 - 0.786 retracement of the leg
    ote_lo = 0.618
    ote_hi = 0.786
    # For UP leg, discount is low position; for DOWN leg, premium is high position
    # We report the retracement depth from the extreme
    if source == "leg" and st.trend == "UP":
        retrace = (hi - price) / (hi - lo) if hi > lo else 0.0
    elif source == "leg" and st.trend == "DOWN":
        retrace = (price - lo) / (hi - lo) if hi > lo else 0.0
    else:
        retrace = min(position, 1.0 - position)

    ote_zone = ote_lo <= retrace <= ote_hi
    if ote_zone:
        pd = "OTE"
    elif (st.trend == "UP" and position < 0.5) or (st.trend == "DOWN" and position > 0.5):
        pd = "DISCOUNT" if st.trend == "UP" else "PREMIUM"
    elif (st.trend == "UP" and position > 0.5) or (st.trend == "DOWN" and position < 0.5):
        pd = "PREMIUM" if st.trend == "UP" else "DISCOUNT"
    else:
        pd = "NEUTRAL"

    dr = DealingRange(high=hi, low=lo, position=position, source=source)
    dr.ote_zone = ote_zone
    dr.premium_discount = pd
    # Attach retrace depth for downstream use (stored in position for compatibility, but we keep extra)
    return dr

