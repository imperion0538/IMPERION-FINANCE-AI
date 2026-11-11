"""Liquidity: where stops rest (pools) and where they were just taken (sweeps).

Pools are equal highs / equal lows, single old highs / lows and the previous day's high / low. A sweep is a wick
through a pool that closes back (same candle) or is reclaimed by the next candle. Detection never looks ahead:
a pool only counts for candle k if its swings were confirmed, and it was still untouched, before k.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ...models import BEARISH, BULLISH, Candle
from ...util import clamp
from .structure import MINOR_RIGHT
from .types import Pool, Swing, Sweep

DAY_MS = 86_400_000
POOL_LOOKBACK = 150
SWEEP_LOOKBACK = 8
MAX_SWEEP_DEPTH_ATR = 2.5

Cluster = Tuple[float, int, int]     # (level, touches, last swing index)


def _clusters(points: Sequence[Swing], tol: float, highs: bool) -> List[Cluster]:
    pts = sorted(points, key=lambda s: s.i)
    used, out = set(), []
    for a, p in enumerate(pts):
        if a in used:
            continue
        group = [p]
        for b in range(a + 1, len(pts)):
            if b not in used and abs(pts[b].price - p.price) <= tol:
                group.append(pts[b])
                used.add(b)
        prices = [s.price for s in group]
        out.append((max(prices) if highs else min(prices), len(group), group[-1].i))
    return out


def _tolerance(price: float, atr: float) -> float:
    return max(0.25 * atr, price * 0.0008)


def previous_day_levels(cs: Sequence[Candle]) -> Optional[Tuple[float, float, int]]:
    """(high, low, index of the last candle) of the previous UTC day, for intraday series that fully cover it."""
    if len(cs) < 3:
        return None
    step = cs[-1].t - cs[-2].t
    if step <= 0 or step > 3_600_000:
        return None
    day0 = cs[-1].t - cs[-1].t % DAY_MS
    idx = [i for i, c in enumerate(cs) if day0 - DAY_MS <= c.t < day0]
    if not idx or cs[idx[0]].t > day0 - DAY_MS + step:
        return None
    return max(cs[i].h for i in idx), min(cs[i].l for i in idx), idx[-1]


def find_pools(cs: Sequence[Candle], minor: Sequence[Swing], atr: float) -> Tuple[List[Pool], List[Pool]]:
    """Pools still resting right now: (above price, nearest first), (below price, nearest first)."""
    n, price = len(cs), cs[-1].c
    tol = _tolerance(price, atr)
    recent = [s for s in minor if s.i >= n - POOL_LOOKBACK]
    above: List[Pool] = []
    below: List[Pool] = []
    for level, touches, last_i in _clusters([s for s in recent if s.kind == "H"], tol, True):
        if level > price and all(c.h <= level for c in cs[last_i + 1:]):
            above.append(Pool(level, "EQH" if touches >= 2 else "HIGH", touches, last_i))
    for level, touches, last_i in _clusters([s for s in recent if s.kind == "L"], tol, False):
        if level < price and all(c.l >= level for c in cs[last_i + 1:]):
            below.append(Pool(level, "EQL" if touches >= 2 else "LOW", touches, last_i))
    pd = previous_day_levels(cs)
    if pd is not None:
        pdh, pdl, last_i = pd
        if pdh > price and all(c.h <= pdh for c in cs[last_i + 1:]):
            above.append(Pool(pdh, "PDH", 2, last_i))
        if pdl < price and all(c.l >= pdl for c in cs[last_i + 1:]):
            below.append(Pool(pdl, "PDL", 2, last_i))
    return sorted(above, key=lambda p: p.price), sorted(below, key=lambda p: -p.price)


def _levels_before(cs: Sequence[Candle], minor: Sequence[Swing], k: int, tol: float, highs: bool) -> List[Tuple[float, int, str]]:
    """Pools as they stood just before candle k: (level, touches, kind)."""
    kind = "H" if highs else "L"
    pts = [s for s in minor if s.kind == kind and s.i + MINOR_RIGHT < k and s.i >= k - POOL_LOOKBACK]
    out: List[Tuple[float, int, str]] = []
    for level, touches, last_i in _clusters(pts, tol, highs):
        between = cs[last_i + 1:k]
        if all((c.h <= level) if highs else (c.l >= level) for c in between):
            out.append((level, touches, ("EQH" if touches >= 2 else "HIGH") if highs else ("EQL" if touches >= 2 else "LOW")))
    pd = previous_day_levels(cs[:k + 1])
    if pd is not None:
        pdh, pdl, last_i = pd
        level = pdh if highs else pdl
        if all((c.h <= level) if highs else (c.l >= level) for c in cs[last_i + 1:k]):
            out.append((level, 2, "PDH" if highs else "PDL"))
    return out


def find_sweeps(cs: Sequence[Candle], minor: Sequence[Swing], atr: float, lookback: int = SWEEP_LOOKBACK) -> List[Sweep]:
    """Sweeps completed in the last `lookback` candles that still hold (price has not lost the level again)."""
    n = len(cs)
    if n < 10 or atr <= 0:
        return []
    price, tol = cs[-1].c, _tolerance(cs[-1].c, atr)
    best: dict = {}
    for k in range(max(MINOR_RIGHT + 3, n - lookback), n):
        c = cs[k]
        nxt = cs[k + 1] if k + 1 < n else None
        rng = max(c.range, 1e-12)
        for highs in (False, True):
            for level, touches, kind in _levels_before(cs, minor, k, tol, highs):
                if highs:
                    if not (c.h > level and c.h - level <= MAX_SWEEP_DEPTH_ATR * atr):
                        continue
                    same = c.c < level
                    reclaimed = same or (nxt is not None and nxt.c < level)
                    extreme = c.h if same or nxt is None else max(c.h, nxt.h)
                    wick = (c.h - max(c.o, c.c)) / rng
                    still_valid, direction = price < level, BEARISH
                else:
                    if not (c.l < level and level - c.l <= MAX_SWEEP_DEPTH_ATR * atr):
                        continue
                    same = c.c > level
                    reclaimed = same or (nxt is not None and nxt.c > level)
                    extreme = c.l if same or nxt is None else min(c.l, nxt.l)
                    wick = (min(c.o, c.c) - c.l) / rng
                    still_valid, direction = price > level, BULLISH
                if not reclaimed or not still_valid:
                    continue
                index = k if same else k + 1
                quality = (0.50 + (0.12 if touches >= 2 else 0.0) + (0.05 if touches >= 3 else 0.0) + (0.04 if kind in ("PDH", "PDL") else 0.0)
                           + 0.10 * clamp(wick / 0.6, 0.0, 1.0) + (0.04 if same else 0.0))
                sw = Sweep(direction, level, extreme, index, n - 1 - index, touches, kind, clamp(quality, 0.5, 0.85))
                key = (direction, round(level / tol))
                old = best.get(key)
                if old is None or (sw.index, sw.quality) > (old.index, old.quality):
                    best[key] = sw
    return sorted(best.values(), key=lambda s: (s.age, -s.quality))
