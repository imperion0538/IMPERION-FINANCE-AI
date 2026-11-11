
"""IMPERION FINANCE AI V17 - Advanced Institutional Zones.

Order blocks, fair value gaps, breakers and mitigation zones.

Preserves the existing V17 public functions and return types.
All signals are calculated from closed candles without future data.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

from ...models import BEARISH, BULLISH, Candle
from .types import Zone

LOOKBACK = 150
DISPLACEMENT_ATR = 1.2
MIN_FVG_ATR = 0.10
MAX_ZONE_ATR = 3.0


def polarity(z: Zone) -> str:
    """Current support/resistance direction."""
    if z.flipped_at is None:
        return z.direction
    return BEARISH if z.direction == BULLISH else BULLISH


def _atr(atrs: Sequence[float], i: int, c: Candle) -> float:
    a = atrs[i]
    if not math.isfinite(a):
        raise ValueError("Non-finite ATR")
    return a if a > 0 else max(c.range, 1e-12)


def _create(
    cs: Sequence[Candle],
    atrs: Sequence[float],
    start: int,
) -> List[Zone]:
    """Create zones only after the displacement/FVG candle has closed."""
    zones: List[Zone] = []

    for i in range(max(start, 5), len(cs)):
        c = cs[i]
        a = _atr(atrs, i, c)
        disp = c.body / a
        body_ratio = c.body / max(c.range, 1e-12)

        if disp >= DISPLACEMENT_ATR and body_ratio >= 0.60:
            previous = cs[i - 5:i]

            if c.bullish and c.c > max(x.h for x in previous):
                ob = next(
                    (x for x in reversed(cs[i - 3:i]) if not x.bullish),
                    None,
                )
                if ob is not None and ob.range <= MAX_ZONE_ATR * a:
                    zones.append(
                        Zone(
                            "OB", BULLISH, ob.l, ob.h,
                            created=i,
                            displacement=round(disp, 2),
                            mean_threshold=(ob.l + ob.h) / 2,
                        )
                    )

            elif not c.bullish and c.c < min(x.l for x in previous):
                ob = next(
                    (x for x in reversed(cs[i - 3:i]) if x.bullish),
                    None,
                )
                if ob is not None and ob.range <= MAX_ZONE_ATR * a:
                    zones.append(
                        Zone(
                            "OB", BEARISH, ob.l, ob.h,
                            created=i,
                            displacement=round(disp, 2),
                            mean_threshold=(ob.l + ob.h) / 2,
                        )
                    )

        c0 = cs[i - 2]
        mid = cs[i - 1]
        am = _atr(atrs, i - 1, mid)

        if (
            c0.h < c.l
            and MIN_FVG_ATR * a <= c.l - c0.h <= MAX_ZONE_ATR * a
        ):
            zones.append(
                Zone(
                    "FVG", BULLISH, c0.h, c.l,
                    created=i,
                    displacement=round(mid.body / am, 2),
                    mean_threshold=(c0.h + c.l) / 2,
                )
            )

        elif (
            c0.l > c.h
            and MIN_FVG_ATR * a <= c0.l - c.h <= MAX_ZONE_ATR * a
        ):
            zones.append(
                Zone(
                    "FVG", BEARISH, c.h, c0.l,
                    created=i,
                    displacement=round(mid.body / am, 2),
                    mean_threshold=(c.h + c0.l) / 2,
                )
            )

    return zones


def _evolve(z: Zone, cs: Sequence[Candle]) -> Optional[Zone]:
    """Replay a zone's lifecycle from its creation.

    FRESH -> TESTED -> FLIPPED -> INVALIDATED

    A close beyond the far boundary flips the zone once.
    A subsequent close back through the opposite boundary kills it.
    """
    inside = False
    width = max(z.hi - z.lo, 1e-12)

    for j in range(z.created + 1, len(cs)):
        x = cs[j]
        touching = x.l <= z.hi and x.h >= z.lo

        if touching and not inside:
            z.touches += 1
        inside = touching

        if z.flipped_at is None:
            if z.direction == BULLISH:
                if x.l < z.hi:
                    z.fill = max(
                        z.fill,
                        min(1.0, (z.hi - x.l) / width),
                    )
                if x.c < z.lo:
                    z.flipped_at = j
                    z.touches = 0
                    inside = False
                    z.state = "FLIPPED"

            else:
                if x.h > z.lo:
                    z.fill = max(
                        z.fill,
                        min(1.0, (x.h - z.lo) / width),
                    )
                if x.c > z.hi:
                    z.flipped_at = j
                    z.touches = 0
                    inside = False
                    z.state = "FLIPPED"

        else:
            if (
                (z.direction == BULLISH and x.c > z.hi)
                or (z.direction == BEARISH and x.c < z.lo)
            ):
                z.state = "INVALIDATED"
                z.invalidated_at = j
                return None

        if z.flipped_at is None:
            z.state = "TESTED" if z.touches > 0 else "FRESH"

    z.quality = zone_quality(z)
    return z


def find_zones(
    cs: Sequence[Candle],
    atrs: Sequence[float],
    lookback: int = LOOKBACK,
) -> List[Zone]:
    """Return active zones, newest first."""
    if len(cs) < 10 or len(atrs) != len(cs) or lookback < 1:
        return []

    created = _create(cs, atrs, len(cs) - lookback)
    live = []

    for zone in created:
        evolved = _evolve(zone, cs)
        if evolved is not None:
            live.append(evolved)

    return sorted(live, key=lambda z: -z.created)


def zones_at_price(
    zones: Sequence[Zone],
    price: float,
    atr: float,
    side: str,
    near_atr: float = 0.25,
) -> List[Zone]:
    """Find valid zones supporting the requested trade direction."""
    if atr <= 0:
        return []

    out = []

    for z in zones:
        if z.state == "INVALIDATED" or polarity(z) != side:
            continue

        if (
            side == BULLISH
            and z.lo - 0.10 * atr <= price <= z.hi + near_atr * atr
        ):
            out.append(z)

        elif (
            side == BEARISH
            and z.lo - near_atr * atr <= price <= z.hi + 0.10 * atr
        ):
            out.append(z)

    return out


def zone_quality(z: Zone) -> float:
    """Score freshness, displacement, mitigation and polarity changes."""
    if z.state == "INVALIDATED":
        return 0.0

    freshness = (
        1.0 if z.touches == 0
        else 0.90 if z.touches == 1
        else 0.65 if z.touches == 2
        else 0.35
    )

    displacement = min(1.0, max(0.0, z.displacement) / 2.2)

    fill_quality = (
        1.0 - 0.65 * max(0.0, min(1.0, z.fill))
        if z.kind == "FVG" and z.flipped_at is None
        else 1.0
    )

    kind_factor = (
        1.05 if z.kind in ("OB", "MITIGATION")
        else 0.95 if z.kind == "BREAKER"
        else 0.90
    )

    flip_factor = 0.85 if z.flipped_at is not None else 1.0

    score = (
        0.40 * freshness
        + 0.35 * displacement
        + 0.25 * fill_quality
    ) * kind_factor * flip_factor

    return max(0.0, min(1.0, score))


def find_mitigation_zones(
    cs: Sequence[Candle],
    atrs: Sequence[float],
    existing: List[Zone],
) -> List[Zone]:
    """Detect strong exits after mitigation of an existing OB/FVG."""
    if len(cs) < 25 or len(atrs) != len(cs):
        return []

    results: List[Zone] = []
    n = len(cs)

    for parent in existing:
        if (
            parent.touches == 0
            or parent.flipped_at is not None
            or parent.kind not in ("OB", "FVG")
            or parent.state == "INVALIDATED"
        ):
            continue

        start = parent.created + 1

        for j in range(start, min(start + 12, n)):
            c = cs[j]
            a = _atr(atrs, j, c)
            disp = c.body / a
            body_ratio = c.body / max(c.range, 1e-12)

            bullish_exit = (
                parent.direction == BULLISH
                and c.bullish
                and c.c > parent.hi + 0.05 * a
            )
            bearish_exit = (
                parent.direction == BEARISH
                and not c.bullish
                and c.c < parent.lo - 0.05 * a
            )

            if not (
                (bullish_exit or bearish_exit)
                and disp >= 1.25
                and body_ratio >= 0.58
            ):
                continue

            rejection = False
            if j > 0:
                prev = cs[j - 1]
                rejection = (
                    prev.l <= parent.hi and prev.c > parent.lo
                    if parent.direction == BULLISH
                    else prev.h >= parent.lo and prev.c < parent.hi
                )

            opposite = next(
                (
                    x for x in reversed(cs[max(0, j - 5):j])
                    if x.bullish != c.bullish
                ),
                None,
            )

            if opposite is None or opposite.range > 2.2 * a:
                continue

            age = n - 1 - j
            q_disp = min(1.0, disp / 2.3)
            q_body = min(1.0, body_ratio / 0.75)
            q_fresh = 1.0 if age <= 3 else 0.8 if age <= 6 else 0.55
            q_parent = min(1.0, parent.quality + 0.1)
            q_rejection = 1.08 if rejection else 1.0

            quality = round(
                min(
                    0.96,
                    (
                        0.32 * q_disp
                        + 0.22 * q_body
                        + 0.18 * q_fresh
                        + 0.18 * q_parent
                    ) * q_rejection,
                ),
                3,
            )

            if quality < 0.62:
                continue

            results.append(
                Zone(
                    kind="MITIGATION",
                    direction=parent.direction,
                    lo=opposite.l,
                    hi=opposite.h,
                    created=j,
                    displacement=round(disp, 2),
                    quality=quality,
                    mean_threshold=(opposite.l + opposite.h) / 2,
                    state="FRESH",
                )
            )
            break

    results.sort(key=lambda z: (-z.quality, -z.created))
    return results[:4]


def describe(z: Zone) -> str:
    """Human-readable zone summary."""
    names = {
        ("OB", False): "OB",
        ("OB", True): "breaker",
        ("FVG", False): "FVG",
        ("FVG", True): "IFVG",
        ("BREAKER", False): "breaker",
        ("BREAKER", True): "breaker",
        ("MITIGATION", False): "mitigation",
        ("MITIGATION", True): "mitigation",
    }

    name = names.get(
        (z.kind, z.flipped_at is not None),
        z.kind.lower(),
    )
    side = "bullish" if polarity(z) == BULLISH else "bearish"

    return (
        f"{side} {name} [{z.lo:.6g}-{z.hi:.6g}] "
        f"touches={z.touches}"
    )
