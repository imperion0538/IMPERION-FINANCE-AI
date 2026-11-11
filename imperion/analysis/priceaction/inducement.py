"""Deep inducement (IDM) detection.

True inducement = internal liquidity (equal highs/lows or minor swings inside the current dealing range)
that is swept (wick through + close back) shortly before a major external liquidity pool is taken.
This is a classic "stop hunt before the real move" pattern.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ...models import BEARISH, BULLISH, Candle
from .types import Inducement, Pool, Sweep, Swing


def _is_swept(cs: Sequence[Candle], level: float, kind: str, start_i: int, end_i: int, atr: float) -> Optional[Tuple[int, float]]:
    """Return (index, extreme) if level was swept with rejection between start_i and end_i."""
    for i in range(max(0, start_i), min(len(cs), end_i + 1)):
        c = cs[i]
        if kind == "L":  # sell-side (below)
            if c.l < level - 0.05 * atr and c.c > level:
                return i, c.l
        else:  # buy-side (above)
            if c.h > level + 0.05 * atr and c.c < level:
                return i, c.h
    return None


def find_inducements(
    cs: Sequence[Candle],
    minor: Sequence[Swing],
    sweeps: List[Sweep],
    pools_above: List[Pool],
    pools_below: List[Pool],
    atr: float,
    dealing_low: Optional[float] = None,
    dealing_high: Optional[float] = None,
) -> List[Inducement]:
    """Detect high-quality internal inducements that precede major sweeps."""
    if len(cs) < 30 or not minor or atr <= 0:
        return []

    n = len(cs)
    out: List[Inducement] = []
    lookback = min(40, n - 10)
    recent_minor = [s for s in minor if s.i >= n - lookback]

    # Build candidate internal levels (equal-ish minor swings)
    tol = max(0.15 * atr, cs[-1].c * 0.0005)
    candidates: List[Tuple[float, str, int]] = []  # price, kind, last_i
    highs = [s for s in recent_minor if s.kind == "H"]
    lows = [s for s in recent_minor if s.kind == "L"]

    for group, kind in ((highs, "H"), (lows, "L")):
        for i, s in enumerate(group):
            cluster = [s]
            for o in group[i + 1:]:
                if abs(o.price - s.price) <= tol and o.i - s.i <= 20:
                    cluster.append(o)
            if len(cluster) >= 2 or (len(cluster) == 1 and s.i >= n - 18):
                price = sum(x.price for x in cluster) / len(cluster)
                # Prefer levels that sit inside the dealing range (true internal)
                if dealing_low is not None and dealing_high is not None:
                    if not (dealing_low + 0.1 * atr < price < dealing_high - 0.1 * atr):
                        continue
                candidates.append((price, kind, max(x.i for x in cluster)))

    # For each major sweep, look for an IDM taken 2-18 candles before it
    for sweep in sweeps:
        if sweep.age > 15:
            continue
        direction = sweep.direction
        # Bullish sweep (sell-side taken) wants a prior sell-side IDM (kind L)
        # Bearish sweep wants prior buy-side IDM (kind H)
        want_kind = "L" if direction == BULLISH else "H"
        search_end = sweep.index - 1  # IDM must occur strictly before the major sweep
        search_start = max(0, sweep.index - 18)

        best: Optional[Inducement] = None
        best_q = 0.0
        for price, kind, last_i in candidates:
            if kind != want_kind:
                continue
            if last_i >= search_end or last_i < search_start - 5:
                continue
            swept = _is_swept(cs, price, kind, search_start, search_end, atr)
            if not swept:
                continue
            sw_i, extreme = swept
            if sw_i >= sweep.index:
                continue
            # Require IDM to sit meaningfully inside, not immediately adjacent to,
            # the external pool. This avoids relabelling the major pool itself.
            major_pools = pools_below if direction == BULLISH else pools_above
            if not major_pools or abs(price - major_pools[0].price) < 0.5 * atr:
                continue
            # One clean rejection: exactly one candle may sweep the level in the
            # IDM-to-major-sweep interval. Repeated taps are not a clean inducement.
            sweep_hits = 0
            for j in range(search_start, sweep.index):
                cj = cs[j]
                hit = (cj.l < price - 0.05 * atr and cj.c > price) if kind == "L" else (cj.h > price + 0.05 * atr and cj.c < price)
                sweep_hits += int(hit)
            if sweep_hits != 1:
                continue
            age = n - 1 - sw_i
            # Quality: closer to major sweep + clean rejection + not too deep
            proximity = max(0.0, 1.0 - abs(sw_i - sweep.index) / 18.0)
            depth_ok = abs(extreme - price) < 1.2 * atr
            q = 0.55 + 0.25 * proximity + (0.15 if depth_ok else 0.0)
            # Bonus if the IDM is clearly internal to major pool
            if abs(price - major_pools[0].price) >= 0.6 * atr:
                q += 0.08
            q = min(0.92, q)
            if q > best_q:
                best_q = q
                best = Inducement(direction, price, sw_i, age, quality=round(q, 3))
        if best and best.quality >= 0.58:
            out.append(best)

    # Deduplicate by level proximity
    unique: List[Inducement] = []
    for idm in sorted(out, key=lambda x: -x.quality):
        if not any(abs(idm.level - u.level) < tol and idm.direction == u.direction for u in unique):
            unique.append(idm)
    return unique[:5]
