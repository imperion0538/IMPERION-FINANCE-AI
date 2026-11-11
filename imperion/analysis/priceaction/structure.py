"""Confirmed swings, close-based structure and directional dealing ranges.

Only pass CLOSED candles, ordered oldest first. A swing becomes available
at index + right. Historical breaks use only swings confirmed at that time.
UP/DOWN breaks remain BOS/CHOCH compatible with the V17 consumers.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

from ...models import Candle
from .types import Break, DealingRange, Structure, Swing

SWING_LEFT = SWING_RIGHT = 3
MINOR_LEFT = MINOR_RIGHT = 2
CHOP_WINDOW = 40
STALE_BREAK = 90
MAJOR_LOOKBACK = 40
RANGE_WINDOW = 60
MIN_RANGE_ATR = 1.5


def _window(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate(cs: Sequence[Candle]) -> None:
    previous_close = None
    for c in cs:
        if not all(math.isfinite(v) for v in (c.t, c.ct, c.o, c.h, c.l, c.c)):
            raise ValueError("non-finite candle")
        if not 0 < c.l <= min(c.o, c.c) <= max(c.o, c.c) <= c.h:
            raise ValueError("invalid OHLC")
        if c.ct < c.t or (previous_close is not None and c.t <= previous_close):
            raise ValueError("candles overlap or are not chronological")
        previous_close = c.ct


def find_swings(
    cs: Sequence[Candle], left: int = SWING_LEFT, right: int = SWING_RIGHT,
) -> List[Swing]:
    """Strict left edge, non-strict right edge: first equal extreme wins.

    Returned indices are pivot locations, NOT confirmation locations.
    A caller replaying history must wait until pivot index + right.
    """
    _window(left, "left")
    _window(right, "right")
    _validate(cs)
    out: List[Swing] = []
    for i in range(left, len(cs) - right):
        hi, lo = cs[i].h, cs[i].l
        before = cs[i - left:i]
        after = cs[i + 1:i + right + 1]
        if all(hi > c.h for c in before) and all(hi >= c.h for c in after):
            out.append(Swing(i, cs[i].t, hi, "H"))
        if all(lo < c.l for c in before) and all(lo <= c.l for c in after):
            out.append(Swing(i, cs[i].t, lo, "L"))
    return out


def analyze_structure(
    cs: Sequence[Candle], swings: Sequence[Swing], atrs: Sequence[float],
    right: int = SWING_RIGHT,
) -> Structure:
    """Wicks and equality never break a level; only a strict close does.

    Confirmation width must match find_swings(..., right=right).
    Each reference is consumed once. Missing/zero ATR falls back to the
    breaking candle's range; non-finite ATR fails instead of making a signal.
    """
    _window(right, "right")
    _validate(cs)
    n = len(cs)
    order = sorted(swings, key=lambda s: (s.i, s.kind))
    identities = set()
    for s in order:
        if isinstance(s.i, bool) or not isinstance(s.i, int) or not 0 <= s.i < n:
            raise ValueError("swing index outside candle series")
        if s.kind not in ("H", "L") or not math.isfinite(s.price):
            raise ValueError("invalid swing")
        expected = cs[s.i].h if s.kind == "H" else cs[s.i].l
        if s.t != cs[s.i].t or s.price != expected:
            raise ValueError("swing does not match its candle")
        identity = (s.i, s.kind)
        if identity in identities:
            raise ValueError("duplicate swing")
        identities.add(identity)

    st = Structure()
    trend = "UNKNOWN"
    prev_high = prev_low = None
    known_highs: List[Swing] = []
    known_lows: List[Swing] = []
    k = 0
    for i, c in enumerate(cs):
        while k < len(order) and order[k].i + right <= i:
            s = order[k]
            k += 1
            if s.kind == "H":
                st.ref_high, prev_high, st.last_high = s, st.last_high, s
                known_highs.append(s)
            else:
                st.ref_low, prev_low, st.last_low = s, st.last_low, s
                known_lows.append(s)
        a = atrs[i] if i < len(atrs) else 0.0
        if not math.isfinite(a):
            raise ValueError("non-finite ATR")
        a = a if a > 0 else max(c.range, 1e-12)
        if st.ref_high is not None and c.c > st.ref_high.price:
            ref = st.ref_high
            origin = min(range(ref.i, i + 1), key=lambda j: cs[j].l)
            major = all(
                ref.price >= s.price for s in known_highs
                if s.i >= i - MAJOR_LOOKBACK
            )
            kind = "CHOCH" if trend == "DOWN" else "BOS"
            st.breaks.append(Break(kind, "UP", ref.price, i, c.body / a, major))
            trend = "UP"
            st.protected, st.protected_i = cs[origin].l, origin
            st.ref_high = None
        elif st.ref_low is not None and c.c < st.ref_low.price:
            ref = st.ref_low
            origin = max(range(ref.i, i + 1), key=lambda j: cs[j].h)
            major = all(
                ref.price <= s.price for s in known_lows
                if s.i >= i - MAJOR_LOOKBACK
            )
            kind = "CHOCH" if trend == "UP" else "BOS"
            st.breaks.append(Break(kind, "DOWN", ref.price, i, c.body / a, major))
            trend = "DOWN"
            st.protected, st.protected_i = cs[origin].h, origin
            st.ref_low = None

    for b in st.breaks:
        b.age = n - 1 - b.index
    st.breaks = st.breaks[-8:]
    st.last_break = st.breaks[-1] if st.breaks else None
    if st.last_break is not None:
        flipping = (
            len(st.breaks) >= 2
            and st.breaks[-1].kind == st.breaks[-2].kind == "CHOCH"
            and st.breaks[-2].age <= CHOP_WINDOW
        )
        st.trend = "RANGE" if flipping or st.last_break.age > STALE_BREAK else trend
    if st.last_high and prev_high and st.last_low and prev_low:
        hh = st.last_high.price > prev_high.price
        hl = st.last_low.price > prev_low.price
        lh = st.last_high.price < prev_high.price
        ll = st.last_low.price < prev_low.price
        st.swing_bias = "HH_HL" if hh and hl else "LH_LL" if lh and ll else "MIXED"
    return st


def dealing_range(
    cs: Sequence[Candle], st: Structure, atr: float,
) -> Optional[DealingRange]:
    """Trend impulse range, otherwise the recent window. OTE is directional.

    position remains low-to-high location, never retracement depth, so V17
    playbooks keep their original interface. OTE requires a directional leg.
    """
    if not cs:
        return None
    _validate(cs)
    if not math.isfinite(atr) or atr <= 0:
        return None
    price = cs[-1].c
    hi = lo = None
    source = "window"
    start = st.protected_i
    valid_origin = (
        isinstance(start, int) and not isinstance(start, bool)
        and 0 <= start < len(cs)
        and st.protected is not None
        and math.isfinite(st.protected)
    )
    if valid_origin and st.trend == "UP":
        lo, hi, source = st.protected, max(c.h for c in cs[start:]), "leg"
    elif valid_origin and st.trend == "DOWN":
        hi, lo, source = st.protected, min(c.l for c in cs[start:]), "leg"
    if hi is None or lo is None or hi - lo < MIN_RANGE_ATR * atr:
        window = cs[-RANGE_WINDOW:]
        hi, lo, source = max(c.h for c in window), min(c.l for c in window), "window"
    if hi <= lo:
        return None
    position = (price - lo) / (hi - lo)
    retrace = None
    if source == "leg" and st.trend == "UP":
        retrace = 1.0 - position
    elif source == "leg" and st.trend == "DOWN":
        retrace = position
    ote = retrace is not None and 0.618 <= retrace <= 0.786
    pd = "NEUTRAL"
    if ote:
        pd = "OTE"
    elif st.trend in ("UP", "DOWN"):
        pd = "DISCOUNT" if position < 0.5 else "PREMIUM" if position > 0.5 else "NEUTRAL"
    return DealingRange(hi, lo, position, source, ote, pd)
