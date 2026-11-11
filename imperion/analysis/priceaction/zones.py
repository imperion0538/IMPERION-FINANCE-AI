"""Points of interest: order blocks, fair-value gaps and what they turn into after price closes through them.

Life cycle of a zone
  created   OB = last opposite candle before a displacement that broke the recent range;
            FVG = three-candle imbalance around a displacement candle.
  visited   every separate entry of price into the zone counts as a touch; fresh zones (0-1 touch) are strongest.
  filled    FVGs track how deep price has traded into them (0..1).
  flipped   a candle CLOSE through the far side turns an OB into a breaker and an FVG into an inverted FVG:
            the zone keeps working, with the opposite polarity.
  dead      a flipped zone that price closes back through has no meaning left and is dropped.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from ...models import BEARISH, BULLISH, Candle
from .types import Zone

LOOKBACK = 150
DISPLACEMENT_ATR = 1.2
MIN_FVG_ATR = 0.10
MAX_ZONE_ATR = 3.0            # wider "zones" are not precise enough to trade from


def polarity(z: Zone) -> str:
    """Side the zone supports NOW (support = BULLISH, resistance = BEARISH)."""
    if z.flipped_at is None:
        return z.direction
    return BEARISH if z.direction == BULLISH else BULLISH


def _create(cs: Sequence[Candle], atrs: Sequence[float], start: int) -> List[Zone]:
    zones: List[Zone] = []
    n = len(cs)
    for i in range(max(start, 5), n):
        c = cs[i]
        a = atrs[i] if atrs[i] > 0 else max(c.range, 1e-12)
        disp = c.body / a
        if disp >= DISPLACEMENT_ATR and c.body >= 0.6 * max(c.range, 1e-12):
            window = cs[i - 5:i]
            if c.bullish and c.c > max(x.h for x in window):
                ob = next((x for x in reversed(cs[i - 3:i]) if not x.bullish), None)
                if ob is not None and ob.range <= MAX_ZONE_ATR * a:
                    zones.append(Zone("OB", BULLISH, ob.l, ob.h, created=i, displacement=round(disp, 2)))
            elif (not c.bullish) and c.c < min(x.l for x in window):
                ob = next((x for x in reversed(cs[i - 3:i]) if x.bullish), None)
                if ob is not None and ob.range <= MAX_ZONE_ATR * a:
                    zones.append(Zone("OB", BEARISH, ob.l, ob.h, created=i, displacement=round(disp, 2)))
        c0, mid = cs[i - 2], cs[i - 1]
        am = atrs[i - 1] if atrs[i - 1] > 0 else a
        if c0.h < c.l and c.l - c0.h >= MIN_FVG_ATR * a and c.l - c0.h <= MAX_ZONE_ATR * a:
            zones.append(Zone("FVG", BULLISH, c0.h, c.l, created=i, displacement=round(mid.body / am, 2)))
        elif c0.l > c.h and c0.l - c.h >= MIN_FVG_ATR * a and c0.l - c.h <= MAX_ZONE_ATR * a:
            zones.append(Zone("FVG", BEARISH, c.h, c0.l, created=i, displacement=round(mid.body / am, 2)))
    return zones


def _evolve(z: Zone, cs: Sequence[Candle]) -> Optional[Zone]:
    """Replay the candles after creation. Returns None when the zone is dead."""
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
                    z.fill = max(z.fill, min(1.0, (z.hi - x.l) / width))
                if x.c < z.lo:
                    z.flipped_at, z.touches, inside = j, 0, False
            else:
                if x.h > z.lo:
                    z.fill = max(z.fill, min(1.0, (x.h - z.lo) / width))
                if x.c > z.hi:
                    z.flipped_at, z.touches, inside = j, 0, False
        else:
            if (z.direction == BULLISH and x.c > z.hi) or (z.direction == BEARISH and x.c < z.lo):
                return None
    return z


def find_zones(cs: Sequence[Candle], atrs: Sequence[float], lookback: int = LOOKBACK) -> List[Zone]:
    """All live zones of the look-back window, newest first."""
    if len(cs) < 10 or len(atrs) != len(cs):
        return []
    out = [z for z in (_evolve(z, cs) for z in _create(cs, atrs, len(cs) - lookback)) if z is not None]
    return sorted(out, key=lambda z: -z.created)


def zones_at_price(zones: Sequence[Zone], price: float, atr: float, side: str, near_atr: float = 0.25) -> List[Zone]:
    """Zones that support `side` (BULLISH = long) and price is in, or just beyond on the reaction side."""
    out = []
    for z in zones:
        if polarity(z) != side:
            continue
        if side == BULLISH and z.lo - 0.10 * atr <= price <= z.hi + near_atr * atr:
            out.append(z)
        elif side == BEARISH and z.lo - near_atr * atr <= price <= z.hi + 0.10 * atr:
            out.append(z)
    return out


def zone_quality(z: Zone) -> float:
    """0..1 advanced quality: fresh, strong displacement, low fill, preferred kinds."""
    fresh = 1.0 if z.touches <= 1 else 0.75 if z.touches == 2 else 0.4
    disp = min(1.0, z.displacement / 2.2)
    filled = 1.0 - 0.65 * z.fill if z.kind == "FVG" and z.flipped_at is None else 1.0
    kind_bonus = 1.05 if z.kind in ("OB", "MITIGATION") else 0.95 if z.kind == "BREAKER" else 0.9
    flip = 0.85 if z.flipped_at is not None else 1.0
    base = 0.40 * fresh + 0.35 * disp + 0.25 * filled
    return max(0.0, min(1.0, base * kind_bonus * flip))


def find_mitigation_zones(cs: Sequence[Candle], atrs: Sequence[float], existing: List[Zone]) -> List[Zone]:
    """Deep mitigation blocks (v2).

    Requirements:
    - Prior OB/FVG must have been touched
    - Strong multi-candle or single strong displacement that cleanly leaves the zone
    - Last opposite candle before the displacement forms the block
    - Quality scored on displacement, body cleanliness, exit cleanliness, age, and parent zone quality
    - Prefer blocks that form after a clear rejection (wick back into zone then leave)
    """
    if len(cs) < 25:
        return []
    mit: List[Zone] = []
    n = len(cs)
    for z in existing:
        if z.touches == 0 or z.flipped_at is not None or z.kind not in ("OB", "FVG"):
            continue
        start = z.created + 1
        for j in range(start, min(start + 12, n)):
            c = cs[j]
            a = atrs[j] if atrs[j] > 0 else 1e-12
            disp = c.body / a
            body_ratio = c.body / max(c.range, 1e-12)
            # Strong single-candle exit
            clean = False
            if z.direction == BULLISH and c.bullish and c.c > z.hi + 0.05 * a and disp >= 1.25 and body_ratio >= 0.58:
                clean = True
            elif z.direction == BEARISH and not c.bullish and c.c < z.lo - 0.05 * a and disp >= 1.25 and body_ratio >= 0.58:
                clean = True
            if not clean:
                continue
            # Optional: look for a small wick rejection into the zone on the previous candle
            rejection = False
            if j > 0:
                prev = cs[j - 1]
                if z.direction == BULLISH and prev.l <= z.hi and prev.c > z.lo:
                    rejection = True
                elif z.direction == BEARISH and prev.h >= z.lo and prev.c < z.hi:
                    rejection = True
            look = cs[max(0, j - 5):j]
            opp = next((x for x in reversed(look) if x.bullish != c.bullish), None)
            if opp is None or opp.range > 2.2 * a:
                continue
            age = n - 1 - j
            q_disp = min(1.0, disp / 2.3)
            q_body = min(1.0, body_ratio / 0.75)
            q_fresh = 1.0 if age <= 3 else 0.8 if age <= 6 else 0.55
            q_parent = min(1.0, z.quality + 0.1)
            q_rej = 1.08 if rejection else 1.0
            quality = round(min(0.96, (0.32 * q_disp + 0.22 * q_body + 0.18 * q_fresh + 0.18 * q_parent) * q_rej), 3)
            if quality < 0.62:
                continue
            mit.append(Zone(
                kind="MITIGATION",
                direction=z.direction,
                lo=opp.l,
                hi=opp.h,
                created=j,
                displacement=round(disp, 2),
                quality=quality,
                mean_threshold=(opp.l + opp.h) / 2.0,
            ))
            break
    mit.sort(key=lambda z: (-z.quality, -z.created))
    return mit[:4]


def describe(z: Zone) -> str:
    name = {("OB", False): "OB", ("OB", True): "breaker", ("FVG", False): "FVG", ("FVG", True): "IFVG", ("BREAKER", False): "breaker", ("BREAKER", True): "breaker", ("MITIGATION", False): "mitigation", ("MITIGATION", True): "mitigation"}.get((z.kind, z.flipped_at is not None), z.kind.lower())
    side = "bullish" if polarity(z) == BULLISH else "bearish"
    return f"{side} {name} [{z.lo:.6g}-{z.hi:.6g}] touches={z.touches}"
