"""
Adapted from user-supplied IMPERION AUTONOMOUS PRO 1.5 RL for V17.
Candlestick volume is uniformly distributed across touched price bins: OHLCV proxy, not exchange price-at-volume.

Volume Profile Analysis (Extended)
- Standard VP
- Session Volume Profiles (Asia / London / NY)
- HVN/LVN + Order Block confluence
- VP-based entry / stop refinement
"""
from __future__ import annotations

from typing import List, Sequence, Dict, Optional, Tuple
from dataclasses import dataclass, field
import math
from datetime import datetime, timezone
from ...models import Candle, BULLISH, BEARISH
from .types import Zone
from enum import Enum

class Session(str, Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OVERLAP = "OVERLAP"          # London + NY
    OFF = "OFF"


class Killzone(str, Enum):
    ASIA_KZ = "ASIA_KZ"              # 00:00 - 04:00 UTC (Tokyo open area)
    LONDON_OPEN = "LONDON_OPEN"      # 07:00 - 10:00 UTC
    LONDON_KZ = "LONDON_KZ"          # 07:00 - 11:00 UTC (classic)
    NY_OPEN = "NY_OPEN"              # 12:00 - 15:00 UTC
    NY_KZ = "NY_KZ"                  # 12:00 - 16:00 UTC
    LONDON_CLOSE = "LONDON_CLOSE"    # 15:00 - 17:00 UTC
    NONE = "NONE"


# UTC saat aralıkları (dakika cinsinden gün başından)
SESSION_WINDOWS = {
    Session.ASIA: (0, 8 * 60),           # 00:00 - 08:00
    Session.LONDON: (7 * 60, 16 * 60),   # 07:00 - 16:00
    Session.NEW_YORK: (12 * 60, 21 * 60),# 12:00 - 21:00
}

KILLZONE_WINDOWS = {
    Killzone.ASIA_KZ: (0, 4 * 60),
    Killzone.LONDON_OPEN: (7 * 60, 10 * 60),
    Killzone.LONDON_KZ: (7 * 60, 11 * 60),
    Killzone.NY_OPEN: (12 * 60, 15 * 60),
    Killzone.NY_KZ: (12 * 60, 16 * 60),
    Killzone.LONDON_CLOSE: (15 * 60, 17 * 60),
}


def _minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def get_session(timestamp_ms: int) -> Session:
    """UTC timestamp (ms) için aktif session."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    mins = _minutes_of_day(dt)

    in_london = SESSION_WINDOWS[Session.LONDON][0] <= mins < SESSION_WINDOWS[Session.LONDON][1]
    in_ny = SESSION_WINDOWS[Session.NEW_YORK][0] <= mins < SESSION_WINDOWS[Session.NEW_YORK][1]
    in_asia = SESSION_WINDOWS[Session.ASIA][0] <= mins < SESSION_WINDOWS[Session.ASIA][1]

    if in_london and in_ny:
        return Session.OVERLAP
    if in_london:
        return Session.LONDON
    if in_ny:
        return Session.NEW_YORK
    if in_asia:
        return Session.ASIA
    return Session.OFF


def get_killzones(timestamp_ms: int) -> List[Killzone]:
    """Aktif killzone(lar). Birden fazla olabilir."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    mins = _minutes_of_day(dt)
    active = []

    for kz, (start, end) in KILLZONE_WINDOWS.items():
        if start <= mins < end:
            active.append(kz)

    return active if active else [Killzone.NONE]





@dataclass
class VolumeNode:
    price: float
    volume: float
    is_poc: bool = False
    is_hvn: bool = False
    is_lvn: bool = False


@dataclass
class VolumeProfile:
    poc: float
    vah: float
    val: float
    total_volume: float
    value_area_volume_pct: float = 0.70
    nodes: List[VolumeNode] = field(default_factory=list)
    hvn_levels: List[float] = field(default_factory=list)
    lvn_levels: List[float] = field(default_factory=list)
    current_position: str = "INSIDE"
    session: Optional[str] = None          # None = composite


def _vp_minutes_of_day(ts_ms: int) -> int:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.hour * 60 + dt.minute


def build_volume_profile(
    candles: Sequence[Candle],
    bins: int = 40,
    value_area_pct: float = 0.70,
    session_filter: Optional[Session] = None,
) -> Optional[VolumeProfile]:
    """Build volume profile. Optionally filter by session."""
    if not isinstance(bins, int) or not 4 <= bins <= 256:
        return None
    if not isinstance(value_area_pct, (int, float)) or not math.isfinite(value_area_pct) or not 0 < value_area_pct <= 1:
        return None
    if len(candles) < 8:
        return None
    # Fail closed on invalid OHLCV input, which would otherwise yield false VP evidence.
    if any(not all(math.isfinite(x) for x in (c.o, c.h, c.l, c.c, c.v))
           or c.h < c.l or c.l <= 0 or c.v < 0 for c in candles):
        return None

    filtered = candles
    if session_filter is not None:
        filtered = [c for c in candles if get_session(c.t) == session_filter]
        if len(filtered) < 5:
            return None

    min_p = min(c.l for c in filtered)
    max_p = max(c.h for c in filtered)
    if max_p <= min_p:
        return None

    bin_size = (max_p - min_p) / bins
    volume_by_bin = [0.0] * bins

    for c in filtered:
        if c.v <= 0 or c.h <= c.l:
            continue
        low_bin = max(0, int((c.l - min_p) / bin_size))
        high_bin = min(bins - 1, int((c.h - min_p) / bin_size))
        span = max(1, high_bin - low_bin + 1)
        vol_per = c.v / span
        for b in range(low_bin, high_bin + 1):
            volume_by_bin[b] += vol_per

    total_vol = sum(volume_by_bin)
    if total_vol <= 0:
        return None

    poc_idx = max(range(bins), key=lambda i: volume_by_bin[i])
    poc_price = min_p + (poc_idx + 0.5) * bin_size

    target = total_vol * value_area_pct
    va_vol = volume_by_bin[poc_idx]
    low_idx = high_idx = poc_idx

    while va_vol < target and (low_idx > 0 or high_idx < bins - 1):
        left = volume_by_bin[low_idx - 1] if low_idx > 0 else 0
        right = volume_by_bin[high_idx + 1] if high_idx < bins - 1 else 0
        if left >= right and low_idx > 0:
            low_idx -= 1
            va_vol += volume_by_bin[low_idx]
        elif high_idx < bins - 1:
            high_idx += 1
            va_vol += volume_by_bin[high_idx]
        else:
            break

    val = min_p + low_idx * bin_size
    vah = min_p + (high_idx + 1) * bin_size

    nodes, hvn, lvn = [], [], []
    avg = total_vol / bins
    for i, vol in enumerate(volume_by_bin):
        price = min_p + (i + 0.5) * bin_size
        node = VolumeNode(price, vol, is_poc=(i == poc_idx))
        if vol > avg * 1.55:
            node.is_hvn = True
            hvn.append(price)
        elif 0 < vol < avg * 0.4:
            node.is_lvn = True
            lvn.append(price)
        nodes.append(node)

    last = filtered[-1].c
    if last > vah:
        pos = "ABOVE_VAH"
    elif last < val:
        pos = "BELOW_VAL"
    elif abs(last - poc_price) / max(poc_price, 1e-8) < 0.0018:
        pos = "AT_POC"
    else:
        pos = "INSIDE"

    return VolumeProfile(
        poc=round(poc_price, 8),
        vah=round(vah, 8),
        val=round(val, 8),
        total_volume=total_vol,
        nodes=nodes,
        hvn_levels=[round(p, 8) for p in sorted(hvn, key=lambda x: -volume_by_bin[int((x - min_p) / bin_size)])[:6]],
        lvn_levels=[round(p, 8) for p in lvn[:6]],
        current_position=pos,
        session=session_filter.value if session_filter else None,
    )


def build_session_profiles(candles: Sequence[Candle], bins: int = 32) -> Dict[str, Optional[VolumeProfile]]:
    """Asia, London, New York ayrı Volume Profile’ları."""
    return {
        "ASIA": build_volume_profile(candles, bins, session_filter=Session.ASIA),
        "LONDON": build_volume_profile(candles, bins, session_filter=Session.LONDON),
        "NEW_YORK": build_volume_profile(candles, bins, session_filter=Session.NEW_YORK),
        "COMPOSITE": build_volume_profile(candles, bins),
    }


def build_killzone_session_vp(candles: Sequence[Candle], bins: int = 24) -> Dict[str, Optional[VolumeProfile]]:
    """
    Session Volume Profile’ı Killzone pencereleriyle birleştirir.
    Sadece ilgili Killzone içinde oluşan hacmi kullanır.
    """
    def filter_kz(c: Candle, kzs: set) -> bool:
        active = set(get_killzones(c.t))
        return bool(active & kzs)

    london_kz = {Killzone.LONDON_OPEN, Killzone.LONDON_KZ}
    ny_kz = {Killzone.NY_OPEN, Killzone.NY_KZ}
    asia_kz = {Killzone.ASIA_KZ}

    def build_for(kz_set, label: str):
        filtered = [c for c in candles if filter_kz(c, kz_set)]
        if len(filtered) < 4:
            return None
        vp = build_volume_profile(filtered, bins)
        if vp:
            vp.session = label
        return vp

    return {
        "LONDON_KZ": build_for(london_kz, "LONDON_KZ"),
        "NY_KZ": build_for(ny_kz, "NY_KZ"),
        "ASIA_KZ": build_for(asia_kz, "ASIA_KZ"),
    }


def order_block_vp_confluence(
    order_blocks: List[Zone],
    vp: Optional[VolumeProfile],
    price: float,
    atr: float,
) -> List[Dict]:
    """
    Order Block’ları HVN/LVN ve Value Area ile skorla.
    Yüksek skorlu OB’ler daha güçlü kabul edilir.
    """
    if not vp or not order_blocks:
        return []

    results = []
    if not math.isfinite(atr) or atr <= 0 or not math.isfinite(price) or price <= 0:
        return []
    tol = atr * 0.35

    for ob in order_blocks:
        if ob.kind != "OB" or not ob.active or ob.flipped_at is not None:
            continue
        score = ob.quality
        reasons = []

        mid = ob.midpoint
        # HVN yakınlığı
        for hvn in vp.hvn_levels:
            if abs(mid - hvn) <= tol or (ob.lo <= hvn <= ob.hi):
                score += 0.18
                reasons.append("HVN")
                break

        # Value Area kenarı
        if abs(mid - vp.val) <= tol or abs(mid - vp.vah) <= tol:
            score += 0.12
            reasons.append("VA_EDGE")

        # POC yakınlığı
        if abs(mid - vp.poc) <= tol * 1.2:
            score += 0.10
            reasons.append("POC")

        # LVN içinde (zayıf bölge – dikkat)
        in_lvn = any(abs(mid - lvn) <= tol for lvn in vp.lvn_levels)
        if in_lvn:
            score -= 0.08
            reasons.append("LVN")

        results.append({
            "order_block": ob,
            "vp_score": round(min(1.0, score), 3),
            "reasons": reasons,
            "preferred": score >= ob.quality + 0.12,
        })

    return sorted(results, key=lambda x: -x["vp_score"])


def refine_entry_stop_with_vp(
    direction: str,
    entry: float,
    stop: float,
    vp: Optional[VolumeProfile],
    atr: float,
) -> Tuple[float, float, str]:
    """
    Entry ve stop’u Volume Profile seviyelerine göre iyileştir.
    Returns: (new_entry, new_stop, note)
    """
    if not vp or direction not in (BULLISH, BEARISH) or atr <= 0 or not math.isfinite(atr):
        return entry, stop, ""

    note = ""
    tol = atr * 0.25

    if direction == BULLISH:
        # Entry’yi VAL veya HVN’ye çekmeye çalış
        candidates = [vp.val, vp.poc] + vp.hvn_levels
        better = [p for p in candidates if p < entry and entry - p < atr * 1.2]
        if better:
            new_entry = max(better)
            if abs(new_entry - entry) > tol * 0.5:
                entry = round(new_entry, 8)
                note += "entry→VP "

        # Stop’u VAL altına veya LVN’ye koy
        if stop > vp.val - tol:
            stop = round(vp.val - atr * 0.15, 8)
            note += "stop→below_VAL"

    else:  # BEARISH
        candidates = [vp.vah, vp.poc] + vp.hvn_levels
        better = [p for p in candidates if p > entry and p - entry < atr * 1.2]
        if better:
            new_entry = min(better)
            if abs(new_entry - entry) > tol * 0.5:
                entry = round(new_entry, 8)
                note += "entry→VP "

        if stop < vp.vah + tol:
            stop = round(vp.vah + atr * 0.15, 8)
            note += "stop→above_VAH"

    return entry, stop, note.strip()


def volume_profile_bias(vp: Optional[VolumeProfile], price: float, trend: str) -> float:
    """Bounded, advisory VP position bias (missing in upstream AUTONOMOUS PRO source).

    This is not a win probability and MUST NOT bypass V17's independent risk gates.
    """
    if vp is None or not math.isfinite(price) or price <= 0:
        return 0.0
    if trend in ("UP", BULLISH) and vp.val <= price <= vp.vah:
        return 0.02
    if trend in ("DOWN", BEARISH) and vp.val <= price <= vp.vah:
        return 0.02
    return 0.0
