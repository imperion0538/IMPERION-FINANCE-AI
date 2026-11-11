"""Deep session liquidity ranges: Asia, London, New York.

Detects session high/low, measures range quality, and scores sweeps (depth + clean rejection).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional, Sequence

from ...models import BEARISH, BULLISH, Candle
from .types import SessionRange


# UTC session windows
SESSIONS = ("ASIA", "LONDON", "NY")
SESSION_ZONES = {"ASIA": "Asia/Tokyo", "LONDON": "Europe/London", "NY": "America/New_York"}
SESSION_HOURS = {"ASIA": (9, 17), "LONDON": (7, 16), "NY": (8, 17)}


def _hour(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour


def killzone_at(ts_ms: int) -> tuple[Optional[str], bool]:
    """Return DST-aware ICT-style killzone label and London/NY overlap flag.

    London open: 07:00-10:00 Europe/London. NY AM: 08:30-11:00
    America/New_York. Overlap is the actual UTC intersection on that date.
    """
    utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    london = utc.astimezone(ZoneInfo("Europe/London"))
    new_york = utc.astimezone(ZoneInfo("America/New_York"))
    in_london = 7 <= london.hour < 10
    ny_minutes = new_york.hour * 60 + new_york.minute
    in_ny = 8 * 60 + 30 <= ny_minutes < 11 * 60
    # Actual cash-session overlap: 13:00-16:00 London and 08:00-11:00 New York.
    london_minutes = london.hour * 60 + london.minute
    in_overlap = 13 * 60 <= london_minutes < 16 * 60 and 8 * 60 <= ny_minutes < 11 * 60
    overlap = in_overlap
    if overlap:
        return "LONDON_NY_OVERLAP", True
    if in_london:
        return "LONDON_OPEN", False
    if in_ny:
        return "NY_OPEN", False
    return None, False


def find_sessions(cs: Sequence[Candle], atr: float) -> List[SessionRange]:
    """Build per-UTC-day session ranges and score post-session sweeps.

    Grouping by date is intentional: equal clock hours from different days must
    never be combined into one synthetic session range.
    """
    if len(cs) < 40 or atr <= 0:
        return []
    n = len(cs)
    step = cs[-1].t - cs[-2].t if len(cs) > 1 else 0
    window_len = min(400, n) if step and step < 900_000 else min(80, n)
    base = n - window_len
    window = cs[-window_len:]

    by_day = {}
    for local_i, candle in enumerate(window):
        day = datetime.fromtimestamp(candle.t / 1000, tz=timezone.utc).date()
        by_day.setdefault(day, []).append((base + local_i, candle))

    out: List[SessionRange] = []
    for day, rows in by_day.items():
        for name in SESSIONS:
            zone = ZoneInfo(SESSION_ZONES[name])
            h_start, h_end = SESSION_HOURS[name]
            selected = []
            for i, c in rows:
                local = datetime.fromtimestamp(c.t / 1000, tz=timezone.utc).astimezone(zone)
                if h_start <= local.hour < h_end:
                    selected.append((i, c))
            if len(selected) < 5:
                continue
            idxs = [i for i, _ in selected]
            candles = [c for _, c in selected]
            sh = max(c.h for c in candles)
            sl = min(c.l for c in candles)
            width = sh - sl
            range_atr = width / atr
            start_i, end_i = idxs[0], idxs[-1]
            age = n - 1 - end_i

            swept = False
            sweep_dir = None
            sweep_q = 0.0
            sweep_index = None
            # Only inspect candles later on the same UTC day. Never let the
            # next day's session candles contaminate this session's sweep.
            same_day_indices = {i for i, _ in rows}
            after = [cs[i] for i in range(end_i + 1, min(n, end_i + 13))
                     if i in same_day_indices]
            if after and range_atr >= 0.8:
                for offset, c in enumerate(after):
                    if c.h > sh + 0.08 * atr and c.c < sh:
                        depth = (c.h - sh) / atr
                        rejection = (c.h - c.c) / max(c.range, 1e-12)
                        sweep_q = min(0.95, 0.45 + 0.25 * min(1.5, depth) + 0.2 * rejection)
                        swept, sweep_dir, sweep_index = True, BEARISH, end_i + 1 + offset
                        break
                if not swept:
                    for offset, c in enumerate(after):
                        if c.l < sl - 0.08 * atr and c.c > sl:
                            depth = (sl - c.l) / atr
                            rejection = (c.c - c.l) / max(c.range, 1e-12)
                            sweep_q = min(0.95, 0.45 + 0.25 * min(1.5, depth) + 0.2 * rejection)
                            swept, sweep_dir, sweep_index = True, BULLISH, end_i + 1 + offset
                            break

            if range_atr < 0.6 and not swept:
                continue
            kz, overlap = killzone_at(cs[sweep_index].t) if sweep_index is not None else (None, False)
            out.append(SessionRange(
                name=name, high=sh, low=sl, start_i=start_i, end_i=end_i,
                swept=swept, sweep_dir=sweep_dir, sweep_quality=round(sweep_q, 3),
                range_atr=round(range_atr, 2), age_candles=age,
                killzone=kz, overlap=overlap,
            ))
    out.sort(key=lambda item: (item.swept, item.sweep_quality, -item.age_candles), reverse=True)
    return out[:3]
