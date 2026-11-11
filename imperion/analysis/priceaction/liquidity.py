
"""IMPERION FINANCE AI V17 - Advanced Liquidity Intelligence.

Confirmed liquidity pools, historical level lifecycle, daily/weekly
reference levels and close-confirmed liquidity sweeps.

Compatibility:
    previous_day_levels(cs)
    find_pools(cs, minor, atr)
    find_sweeps(cs, minor, atr, lookback=8)

All computations are stateless. Inputs must be chronological CLOSED candles.
No future candle may confirm a historical sweep or pivot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ...models import BEARISH, BULLISH, Candle
from ...util import clamp
from .structure import MINOR_RIGHT
from .types import Pool, Swing, Sweep

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
POOL_LOOKBACK = 150
SWEEP_LOOKBACK = 8
MAX_SWEEP_DEPTH_ATR = 2.5

Cluster = Tuple[float, int, int]

FRESH = "FRESH"
TESTED = "TESTED"
SWEPT = "SWEPT"
INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class LiquidityLevel:
    price: float
    kind: str
    side: str
    state: str
    touches: int
    created_i: int
    last_test_i: Optional[int] = None
    swept_i: Optional[int] = None
    invalidated_i: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "kind": self.kind,
            "side": self.side,
            "state": self.state,
            "touches": self.touches,
            "created_i": self.created_i,
            "last_test_i": self.last_test_i,
            "swept_i": self.swept_i,
            "invalidated_i": self.invalidated_i,
        }


def _tolerance(price: float, atr: float) -> float:
    if not math.isfinite(price) or not math.isfinite(atr):
        raise ValueError("Non-finite liquidity inputs")
    if price <= 0 or atr <= 0:
        raise ValueError("Price and ATR must be positive")
    return max(0.25 * atr, price * 0.0008)


def _clusters(
    points: Sequence[Swing],
    tol: float,
    highs: bool,
) -> List[Cluster]:
    """Cluster equal extremes without changing the V17 return format."""
    pts = sorted(points, key=lambda s: s.i)
    used = set()
    out: List[Cluster] = []

    for a, p in enumerate(pts):
        if a in used:
            continue

        group = [p]
        used.add(a)

        for b in range(a + 1, len(pts)):
            if b in used:
                continue
            if abs(pts[b].price - p.price) <= tol:
                group.append(pts[b])
                used.add(b)

        prices = [s.price for s in group]
        level = max(prices) if highs else min(prices)
        out.append((level, len(group), group[-1].i))

    return out


def _reference_period(
    cs: Sequence[Candle],
    period_ms: int,
    offset_ms: int = 0,
) -> Optional[Tuple[float, float, int]]:
    """Return a completed previous UTC period.

    Reject missing leading or trailing bars rather than treating partial
    periods as reliable daily/weekly reference levels.
    """
    if len(cs) < 3:
        return None

    step = cs[-1].t - cs[-2].t
    if step <= 0 or step > 3_600_000:
        return None

    if any(
        cs[i].t - cs[i - 1].t != step
        for i in range(1, len(cs))
    ):
        return None

    current_start = (
        ((cs[-1].t - offset_ms) // period_ms) * period_ms
        + offset_ms
    )
    previous_start = current_start - period_ms

    indices = [
        i for i, c in enumerate(cs)
        if previous_start <= c.t < current_start
    ]
    if not indices:
        return None

    first = cs[indices[0]]
    last = cs[indices[-1]]

    if first.t != previous_start:
        return None
    if last.t + step != current_start:
        return None

    return (
        max(cs[i].h for i in indices),
        min(cs[i].l for i in indices),
        indices[-1],
    )


def previous_day_levels(
    cs: Sequence[Candle],
) -> Optional[Tuple[float, float, int]]:
    """Previous completed UTC day: PDH, PDL, final candle index."""
    return _reference_period(cs, DAY_MS)


def previous_week_levels(
    cs: Sequence[Candle],
) -> Optional[Tuple[float, float, int]]:
    """Previous completed UTC Monday-Sunday week: PWH, PWL, index."""
    monday_offset = 4 * DAY_MS
    return _reference_period(cs, WEEK_MS, monday_offset)


def _reference_pools(
    cs: Sequence[Candle],
    k: int,
    highs: bool,
) -> List[Tuple[float, int, str, int]]:
    out = []
    history = cs[:k + 1]

    for result, high_kind, low_kind in (
        (previous_day_levels(history), "PDH", "PDL"),
        (previous_week_levels(history), "PWH", "PWL"),
    ):
        if result is None:
            continue

        high, low, last_i = result
        level = high if highs else low
        kind = high_kind if highs else low_kind
        out.append((level, 2, kind, last_i))

    return out


def _levels_before(
    cs: Sequence[Candle],
    minor: Sequence[Swing],
    k: int,
    tol: float,
    highs: bool,
) -> List[Tuple[float, int, str]]:
    """Levels that existed strictly before candle k."""
    kind = "H" if highs else "L"

    pts = [
        s for s in minor
        if s.kind == kind
        and s.i + MINOR_RIGHT < k
        and s.i >= k - POOL_LOOKBACK
    ]

    out: List[Tuple[float, int, str]] = []

    for level, touches, last_i in _clusters(pts, tol, highs):
        between = cs[last_i + 1:k]
        untouched = all(
            c.h <= level if highs else c.l >= level
            for c in between
        )
        if untouched:
            label = (
                "EQH" if touches >= 2 else "HIGH"
            ) if highs else (
                "EQL" if touches >= 2 else "LOW"
            )
            out.append((level, touches, label))

    for level, touches, label, last_i in _reference_pools(
        cs, k, highs
    ):
        between = cs[last_i + 1:k]
        untouched = all(
            c.h <= level if highs else c.l >= level
            for c in between
        )
        if untouched:
            out.append((level, touches, label))

    return out


def find_pools(
    cs: Sequence[Candle],
    minor: Sequence[Swing],
    atr: float,
) -> Tuple[List[Pool], List[Pool]]:
    """Unconsumed liquidity above/below the current closing price."""
    if not cs or atr <= 0:
        return [], []

    n = len(cs)
    price = cs[-1].c
    tol = _tolerance(price, atr)

    recent = [
        s for s in minor
        if s.i >= n - POOL_LOOKBACK
        and s.i + MINOR_RIGHT < n
    ]

    above: List[Pool] = []
    below: List[Pool] = []

    for highs in (True, False):
        pts = [s for s in recent if s.kind == ("H" if highs else "L")]

        for level, touches, last_i in _clusters(pts, tol, highs):
            untouched = all(
                c.h <= level if highs else c.l >= level
                for c in cs[last_i + 1:]
            )
            if not untouched:
                continue

            label = (
                "EQH" if touches >= 2 else "HIGH"
            ) if highs else (
                "EQL" if touches >= 2 else "LOW"
            )

            if highs and level > price:
                above.append(Pool(level, label, touches, last_i))
            elif not highs and level < price:
                below.append(Pool(level, label, touches, last_i))

        for level, touches, label, last_i in _reference_pools(
            cs, n - 1, highs
        ):
            untouched = all(
                c.h <= level if highs else c.l >= level
                for c in cs[last_i + 1:]
            )
            if not untouched:
                continue

            if highs and level > price:
                above.append(Pool(level, label, touches, last_i))
            elif not highs and level < price:
                below.append(Pool(level, label, touches, last_i))

    return (
        sorted(above, key=lambda p: p.price),
        sorted(below, key=lambda p: -p.price),
    )


def find_sweeps(
    cs: Sequence[Candle],
    minor: Sequence[Swing],
    atr: float,
    lookback: int = SWEEP_LOOKBACK,
) -> List[Sweep]:
    """Detect reclaimed liquidity sweeps without future-data leakage."""
    n = len(cs)
    if n < 10 or atr <= 0 or lookback < 1:
        return []

    price = cs[-1].c
    tol = _tolerance(price, atr)
    best: Dict[tuple, Sweep] = {}

    start = max(MINOR_RIGHT + 3, n - lookback)

    for k in range(start, n):
        c = cs[k]
        nxt = cs[k + 1] if k + 1 < n else None
        rng = max(c.range, 1e-12)

        for highs in (False, True):
            for level, touches, kind in _levels_before(
                cs, minor, k, tol, highs
            ):
                if highs:
                    depth = c.h - level
                    if not 0 < depth <= MAX_SWEEP_DEPTH_ATR * atr:
                        continue

                    same = c.c < level
                    reclaimed = same or (
                        nxt is not None and nxt.c < level
                    )
                    extreme = (
                        c.h if same or nxt is None
                        else max(c.h, nxt.h)
                    )
                    wick = (c.h - max(c.o, c.c)) / rng
                    valid = price < level
                    direction = BEARISH
                else:
                    depth = level - c.l
                    if not 0 < depth <= MAX_SWEEP_DEPTH_ATR * atr:
                        continue

                    same = c.c > level
                    reclaimed = same or (
                        nxt is not None and nxt.c > level
                    )
                    extreme = (
                        c.l if same or nxt is None
                        else min(c.l, nxt.l)
                    )
                    wick = (min(c.o, c.c) - c.l) / rng
                    valid = price > level
                    direction = BULLISH

                if not reclaimed or not valid:
                    continue

                index = k if same else k + 1

                quality = (
                    0.50
                    + (0.12 if touches >= 2 else 0.0)
                    + (0.05 if touches >= 3 else 0.0)
                    + (
                        0.04 if kind in (
                            "PDH", "PDL", "PWH", "PWL"
                        ) else 0.0
                    )
                    + 0.10 * clamp(wick / 0.6, 0.0, 1.0)
                    + (0.04 if same else 0.0)
                )

                sw = Sweep(
                    direction,
                    level,
                    extreme,
                    index,
                    n - 1 - index,
                    touches,
                    kind,
                    clamp(quality, 0.5, 0.85),
                )

                key = (direction, round(level / tol))
                old = best.get(key)

                if old is None or (
                    sw.index, sw.quality
                ) > (
                    old.index, old.quality
                ):
                    best[key] = sw

    return sorted(
        best.values(),
        key=lambda s: (s.age, -s.quality),
    )


def level_lifecycle(
    cs: Sequence[Candle],
    level: float,
    highs: bool,
    created_i: int,
    tolerance: float = 0.0,
) -> dict:
    """Classify a historical level using only candles after creation.

    FRESH: no subsequent interaction.
    TESTED: approached/touched without crossing.
    SWEPT: crossed by wick and reclaimed at close.
    INVALIDATED: closed beyond the level.

    INVALIDATED is terminal. A new reclaimed level requires a new
    lifecycle rather than silently reviving an invalidated one.
    """
    if (
        not cs
        or not math.isfinite(level)
        or level <= 0
        or not 0 <= created_i < len(cs)
        or tolerance < 0
        or not math.isfinite(tolerance)
    ):
        raise ValueError("Invalid liquidity lifecycle inputs")

    state = FRESH
    tests = 0
    last_test = None
    swept_at = None
    invalidated_at = None

    for i in range(created_i + 1, len(cs)):
        c = cs[i]

        crossed = c.h > level if highs else c.l < level
        closed_beyond = c.c > level if highs else c.c < level

        if closed_beyond:
            state = INVALIDATED
            invalidated_at = i
            break

        if crossed:
            state = SWEPT
            swept_at = i
            tests += 1
            last_test = i
            continue

        near = (
            c.h >= level - tolerance
            if highs else c.l <= level + tolerance
        )

        if near:
            tests += 1
            last_test = i
            if state == FRESH:
                state = TESTED

    return {
        "state": state,
        "tests": tests,
        "last_test_i": last_test,
        "swept_i": swept_at,
        "invalidated_i": invalidated_at,
    }


def liquidity_inventory(
    cs: Sequence[Candle],
    minor: Sequence[Swing],
    atr: float,
) -> List[LiquidityLevel]:
    """Audit confirmed swing liquidity and its subsequent lifecycle.

    Includes consumed historical levels, unlike find_pools(), which
    intentionally returns only still-resting pools.
    """
    if not cs or atr <= 0:
        return []

    tol = _tolerance(cs[-1].c, atr)
    n = len(cs)
    levels: List[LiquidityLevel] = []

    for highs in (True, False):
        kind = "H" if highs else "L"
        points = [
            s for s in minor
            if s.kind == kind
            and s.i + MINOR_RIGHT < n
            and s.i >= n - POOL_LOOKBACK
        ]

        for price, touches, last_i in _clusters(
            points, tol, highs
        ):
            lifecycle = level_lifecycle(
                cs,
                price,
                highs,
                min(last_i + MINOR_RIGHT, n - 1),
                tolerance=tol * 0.25,
            )

            label = (
                "EQH" if touches >= 2 else "HIGH"
            ) if highs else (
                "EQL" if touches >= 2 else "LOW"
            )

            levels.append(
                LiquidityLevel(
                    price=price,
                    kind=label,
                    side="BUY_SIDE" if highs else "SELL_SIDE",
                    state=lifecycle["state"],
                    touches=touches,
                    created_i=last_i,
                    last_test_i=lifecycle["last_test_i"],
                    swept_i=lifecycle["swept_i"],
                    invalidated_i=lifecycle["invalidated_i"],
                )
            )

    return sorted(
        levels,
        key=lambda x: (x.price, x.kind),
    )
