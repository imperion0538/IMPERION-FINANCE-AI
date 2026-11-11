"""Market state: regime, volatility, momentum, trading session and universe breadth.

These readings describe the MARKET the playbooks operate in. The regime decides which playbooks fit and how the
director weighs its agents; volatility tightens thresholds when the market is disorderly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence

from ..models import BEARISH, BULLISH, NEUTRAL, Candle
from ..util import clamp, now_ms
from .indicators import adx, atr_series, bb_width, efficiency_ratio, ema, macd_hist, percentile_rank, rsi

REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "TRANSITION", "VOLATILE", "UNKNOWN")


def volatility_state(cs: Sequence[Candle]) -> Dict[str, Any]:
    """Where current volatility sits in its own history: SQUEEZE / NORMAL / EXPANSION (non-directional)."""
    out: Dict[str, Any] = {"state": "UNKNOWN", "atr_pctile": None, "bbw_pctile": None}
    if len(cs) < 140:
        return out
    series = atr_series(cs)
    atr_pct = [a / c.c for a, c in zip(series, cs) if c.c > 0][-200:]
    closes = [c.c for c in cs]
    widths = [w for w in (bb_width(closes[: i + 1]) for i in range(len(closes) - 120, len(closes))) if w is not None]
    if not atr_pct or not widths:
        return out
    ap, bp = percentile_rank(atr_pct[-1], atr_pct[:-1]), percentile_rank(widths[-1], widths[:-1])
    state = "EXPANSION" if ap >= 0.90 else "SQUEEZE" if bp <= 0.15 else "NORMAL"
    return {"state": state, "atr_pctile": round(ap, 3), "bbw_pctile": round(bp, 3)}


def regime(cs: Sequence[Candle], structure_trend: Optional[str] = None) -> Dict[str, Any]:
    """TREND_UP / TREND_DOWN / RANGE / TRANSITION / VOLATILE from efficiency ratio, ADX and price-action structure."""
    out: Dict[str, Any] = {"regime": "UNKNOWN", "er": None, "adx": None}
    if len(cs) < 130:
        return out
    closes = [c.c for c in cs]
    er = efficiency_ratio(closes, 30) or 0.0
    a = adx(cs)
    adx_v, pdi, mdi = a if a else (0.0, 0.0, 0.0)
    vol = volatility_state(cs)
    up = closes[-1] > closes[-31]
    if vol["state"] == "EXPANSION" and er < 0.25:
        name = "VOLATILE"                    # wide swings without progress
    elif (adx_v >= 25 and er >= 0.25) or (er >= 0.35 and structure_trend in ("UP", "DOWN")):
        trend_up = pdi >= mdi if adx_v >= 25 else up
        if structure_trend == "UP":
            trend_up = True
        elif structure_trend == "DOWN":
            trend_up = False
        name = "TREND_UP" if trend_up else "TREND_DOWN"
    elif (adx_v < 20 and er < 0.20) or structure_trend == "RANGE":
        name = "RANGE"
    else:
        name = "TRANSITION"
    out.update(regime=name, er=round(er, 3), adx=round(adx_v, 1), plus_di=round(pdi, 1), minus_di=round(mdi, 1), volatility=vol["state"],
               atr_pctile=vol["atr_pctile"])
    return out


def momentum(cs: Sequence[Candle]) -> Dict[str, Any]:
    """RSI + MACD-histogram + ADX/DI reading. `exhausted` marks RSI extremes (late to chase)."""
    if len(cs) < 60:
        return {"available": False}
    closes = [c.c for c in cs]
    r, hist = rsi(closes), macd_hist(closes)
    a = adx(cs)
    if r is None or len(hist) < 4:
        return {"available": False}
    h, h_prev = hist[-1], hist[-4]
    score = 0.0
    if h > 0 and h >= h_prev:
        score += 0.4
    elif h < 0 and h <= h_prev:
        score -= 0.4
    score += clamp((r - 50) / 25, -1, 1) * 0.35
    if a:
        adx_v, pdi, mdi = a
        if adx_v >= 20:
            score += 0.25 * (1 if pdi > mdi else -1) * clamp((adx_v - 20) / 20, 0.2, 1.0)
    return {"available": True, "rsi": round(r, 1), "macd_hist": h, "score": round(clamp(score, -1, 1), 3),
            "exhausted": "UP" if r >= 78 else "DOWN" if r <= 22 else None, "adx": round(a[0], 1) if a else None}


def session(ts_ms: Optional[int] = None) -> str:
    hour = datetime.fromtimestamp((ts_ms if ts_ms is not None else now_ms()) / 1000, tz=timezone.utc).hour
    if 12 <= hour < 16:
        return "LONDON_NY_OVERLAP"
    if 7 <= hour < 12:
        return "LONDON"
    if 16 <= hour < 21:
        return "NEW_YORK"
    if 0 <= hour < 7:
        return "ASIA"
    return "OFF_HOURS"


def breadth(candles, symbols: Iterable[str], tf: str, min_symbols: int = 5) -> Dict[str, Any]:
    """Share of the universe trading above its EMA50 on `tf` (risk-on / risk-off backdrop)."""
    above = total = 0
    for sym in symbols:
        closes = [c.c for c in candles.get(sym, tf, 120)]
        e = ema(closes, 50)
        if e is None:
            continue
        total += 1
        above += closes[-1] > e
    if total < min_symbols:
        return {"available": False, "symbols": total}
    share = above / total
    direction = BULLISH if share >= 0.70 else BEARISH if share <= 0.30 else NEUTRAL
    return {"available": True, "share": round(share, 3), "symbols": total, "direction": direction, "tf": tf}
