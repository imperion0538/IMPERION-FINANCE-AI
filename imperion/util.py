"""Small shared helpers: time/timeframe math, safe numerics, logging."""
from __future__ import annotations

import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}
# The Unix epoch is a Thursday; Binance weekly candles open on Monday 00:00 UTC (+4 days).
_WEEK_OFFSET_MS = 4 * 86400 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


def tf_ms(tf: str) -> int:
    return TF_SECONDS[tf] * 1000


def candle_open_for(ts_ms: int, tf: str) -> int:
    """Open time of the candle that contains ts_ms."""
    step = tf_ms(tf)
    if tf == "1w":
        return ((ts_ms - _WEEK_OFFSET_MS) // step) * step + _WEEK_OFFSET_MS
    return (ts_ms // step) * step


def last_closed_open(ts_ms: int, tf: str) -> int:
    """Open time of the most recent fully closed candle at ts_ms."""
    return candle_open_for(ts_ms, tf) - tf_ms(tf)


def fnum(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Finite float or default. Booleans and NaN/inf are rejected."""
    if isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def iso(ts_ms: Optional[int] = None) -> str:
    ts = (ts_ms if ts_ms is not None else now_ms()) / 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def utc_day(ts_ms: Optional[int] = None) -> str:
    ts = (ts_ms if ts_ms is not None else now_ms()) / 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def get_tz(name: str):
    """ZoneInfo with a fixed-offset fallback (Turkey has had no DST since 2016: UTC+3)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        if name in ("Europe/Istanbul", "Turkey", "TRT"):
            return timezone(timedelta(hours=3))
        return timezone.utc


def setup_logging(level: str = "INFO") -> logging.Logger:
    root = logging.getLogger("imperion")
    if root.handlers:
        root.setLevel(level.upper())
        return root
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s", "%Y-%m-%dT%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False
    return root


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")
