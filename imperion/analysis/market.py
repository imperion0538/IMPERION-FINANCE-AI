
"""IMPERION FINANCE AI V17 - Market Regime Intelligence.

Market regime, volatility, momentum, UTC session and breadth analysis.

Public V17 interfaces are preserved:
    volatility_state(cs)
    regime(cs, structure_trend=None)
    momentum(cs)
    session(ts_ms=None)
    breadth(candles, symbols, tf, min_symbols=5)

New outputs are additive. All calculations use the supplied candle
history only; no future candles or hidden persistent state.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence

from ..models import BEARISH, BULLISH, NEUTRAL, Candle
from ..util import clamp, now_ms
from .indicators import (
    adx,
    atr_series,
    bb_width,
    efficiency_ratio,
    ema,
    macd_hist,
    percentile_rank,
    rsi,
)

REGIMES = (
    "TREND_UP",
    "TREND_DOWN",
    "RANGE",
    "TRANSITION",
    "VOLATILE",
    "UNKNOWN",
)

MIN_REGIME_CANDLES = 130
MIN_VOLATILITY_CANDLES = 140
TREND_ADX = 25.0
RANGE_ADX = 20.0
TREND_ER = 0.25
STRONG_ER = 0.35
RANGE_ER = 0.20


def _valid_closes(cs: Sequence[Candle]) -> bool:
    return bool(cs) and all(
        math.isfinite(c.c) and c.c > 0 for c in cs
    )


def _safe_number(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _confidence(
    name: str,
    er: float,
    adx_value: float,
    structure_trend: Optional[str],
    plus_di: float,
    minus_di: float,
) -> float:
    """Evidence score, not a calibrated win probability."""
    if name == "UNKNOWN":
        return 0.0

    if name.startswith("TREND_"):
        direction = (
            "UP" if name == "TREND_UP" else "DOWN"
        )
        er_score = clamp(er / 0.50, 0.0, 1.0)
        adx_score = clamp(
            (adx_value - 15.0) / 25.0, 0.0, 1.0
        )
        di_score = clamp(
            abs(plus_di - minus_di) / 25.0, 0.0, 1.0
        )
        structure_score = (
            1.0 if structure_trend == direction
            else 0.35 if structure_trend in (None, "UNKNOWN")
            else 0.0
        )
        value = (
            0.35 * er_score
            + 0.25 * adx_score
            + 0.20 * di_score
            + 0.20 * structure_score
        )

    elif name == "RANGE":
        value = (
            0.55 * (1.0 - clamp(er / 0.35, 0.0, 1.0))
            + 0.45 * (
                1.0 - clamp(adx_value / 35.0, 0.0, 1.0)
            )
        )

    elif name == "VOLATILE":
        value = (
            0.60
            + 0.20 * (
                1.0 - clamp(er / 0.25, 0.0, 1.0)
            )
        )

    else:
        value = 0.45

    return round(clamp(value, 0.0, 1.0), 3)


def volatility_state(
    cs: Sequence[Candle],
) -> Dict[str, Any]:
    """Relative ATR and Bollinger width volatility regime."""
    out: Dict[str, Any] = {
        "state": "UNKNOWN",
        "atr_pctile": None,
        "bbw_pctile": None,
    }

    if (
        len(cs) < MIN_VOLATILITY_CANDLES
        or not _valid_closes(cs)
    ):
        return out

    series = atr_series(cs)

    atr_pct = [
        a / c.c
        for a, c in zip(series, cs)
        if c.c > 0 and math.isfinite(a) and a >= 0
    ][-200:]

    closes = [c.c for c in cs]
    widths = []

    for i in range(len(closes) - 120, len(closes)):
        width = _safe_number(bb_width(closes[:i + 1]))
        if width is not None:
            widths.append(width)

    if len(atr_pct) < 2 or len(widths) < 2:
        return out

    ap = _safe_number(
        percentile_rank(atr_pct[-1], atr_pct[:-1])
    )
    bp = _safe_number(
        percentile_rank(widths[-1], widths[:-1])
    )

    if ap is None or bp is None:
        return out

    if ap >= 0.90:
        state = "EXPANSION"
    elif bp <= 0.15:
        state = "SQUEEZE"
    else:
        state = "NORMAL"

    return {
        "state": state,
        "atr_pctile": round(ap, 3),
        "bbw_pctile": round(bp, 3),
    }


def regime(
    cs: Sequence[Candle],
    structure_trend: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify market regime using ADX, ER and structure evidence."""
    out: Dict[str, Any] = {
        "regime": "UNKNOWN",
        "er": None,
        "adx": None,
    }

    if (
        len(cs) < MIN_REGIME_CANDLES
        or not _valid_closes(cs)
    ):
        return out

    closes = [c.c for c in cs]
    er = _safe_number(efficiency_ratio(closes, 30))
    adx_result = adx(cs)

    if er is None:
        er = 0.0

    if adx_result is None:
        adx_value = pdi = mdi = 0.0
    else:
        adx_value, pdi, mdi = adx_result
        values = (
            _safe_number(adx_value),
            _safe_number(pdi),
            _safe_number(mdi),
        )
        if any(v is None for v in values):
            return out
        adx_value, pdi, mdi = values

    vol = volatility_state(cs)
    structure = str(structure_trend or "").upper()

    price_up = closes[-1] > closes[-31]
    di_up = pdi >= mdi

    volatile = (
        vol["state"] == "EXPANSION"
        and er < 0.25
    )

    trending = (
        (adx_value >= TREND_ADX and er >= TREND_ER)
        or (
            er >= STRONG_ER
            and structure in ("UP", "DOWN")
        )
    )

    ranging = (
        (adx_value < RANGE_ADX and er < RANGE_ER)
        or structure == "RANGE"
    )

    if volatile:
        name = "VOLATILE"

    elif trending:
        trend_up = (
            di_up if adx_value >= TREND_ADX else price_up
        )

        if structure == "UP":
            trend_up = True
        elif structure == "DOWN":
            trend_up = False

        name = "TREND_UP" if trend_up else "TREND_DOWN"

    elif ranging:
        name = "RANGE"

    else:
        name = "TRANSITION"

    confidence = _confidence(
        name,
        er,
        adx_value,
        structure,
        pdi,
        mdi,
    )

    direction = (
        "UP" if name == "TREND_UP"
        else "DOWN" if name == "TREND_DOWN"
        else "NEUTRAL"
    )

    conflict = (
        structure in ("UP", "DOWN")
        and direction in ("UP", "DOWN")
        and structure != direction
    )

    out.update(
        regime=name,
        er=round(er, 3),
        adx=round(adx_value, 1),
        plus_di=round(pdi, 1),
        minus_di=round(mdi, 1),
        volatility=vol["state"],
        atr_pctile=vol["atr_pctile"],
        confidence=confidence,
        direction=direction,
        structure_conflict=conflict,
        trade_caution=name in (
            "TRANSITION",
            "VOLATILE",
            "UNKNOWN",
        ),
    )

    return out


def momentum(cs: Sequence[Candle]) -> Dict[str, Any]:
    """RSI, MACD and ADX directional momentum evidence."""
    if len(cs) < 60 or not _valid_closes(cs):
        return {"available": False}

    closes = [c.c for c in cs]
    r = _safe_number(rsi(closes))
    hist = macd_hist(closes)
    a = adx(cs)

    if r is None or len(hist) < 4:
        return {"available": False}

    h = _safe_number(hist[-1])
    previous = _safe_number(hist[-4])

    if h is None or previous is None:
        return {"available": False}

    score = 0.0

    if h > 0 and h >= previous:
        score += 0.4
    elif h < 0 and h <= previous:
        score -= 0.4

    score += clamp(
        (r - 50.0) / 25.0, -1.0, 1.0
    ) * 0.35

    adx_value = None

    if a:
        av, pdi, mdi = a
        if all(
            _safe_number(v) is not None
            for v in (av, pdi, mdi)
        ):
            adx_value = av
            if av >= 20:
                score += (
                    0.25
                    * (1 if pdi > mdi else -1)
                    * clamp(
                        (av - 20) / 20,
                        0.2,
                        1.0,
                    )
                )

    score = round(clamp(score, -1.0, 1.0), 3)

    direction = (
        BULLISH if score >= 0.20
        else BEARISH if score <= -0.20
        else NEUTRAL
    )

    return {
        "available": True,
        "rsi": round(r, 1),
        "macd_hist": h,
        "score": score,
        "exhausted": (
            "UP" if r >= 78
            else "DOWN" if r <= 22
            else None
        ),
        "adx": (
            round(adx_value, 1)
            if adx_value is not None
            else None
        ),
        "direction": direction,
        "strength": round(abs(score), 3),
    }


def session(ts_ms: Optional[int] = None) -> str:
    """Legacy UTC session labels.

    These fixed UTC windows are approximate and do not implement
    daylight-saving transitions. A calendar-aware session module
    is required before treating them as exact exchange sessions.
    """
    timestamp = ts_ms if ts_ms is not None else now_ms()

    hour = datetime.fromtimestamp(
        timestamp / 1000,
        tz=timezone.utc,
    ).hour

    if 12 <= hour < 16:
        return "LONDON_NY_OVERLAP"
    if 7 <= hour < 12:
        return "LONDON"
    if 16 <= hour < 21:
        return "NEW_YORK"
    if 0 <= hour < 7:
        return "ASIA"

    return "OFF_HOURS"


def breadth(
    candles,
    symbols: Iterable[str],
    tf: str,
    min_symbols: int = 5,
) -> Dict[str, Any]:
    """Market breadth based on EMA50 across available symbols."""
    above = 0
    total = 0

    for symbol in symbols:
        history = candles.get(symbol, tf, 120)
        closes = [c.c for c in history]

        if not _valid_closes(history):
            continue

        average = _safe_number(ema(closes, 50))

        if average is None:
            continue

        total += 1
        if closes[-1] > average:
            above += 1

    if total < min_symbols:
        return {
            "available": False,
            "symbols": total,
        }

    share = above / total

    direction = (
        BULLISH if share >= 0.70
        else BEARISH if share <= 0.30
        else NEUTRAL
    )

    return {
        "available": True,
        "share": round(share, 3),
        "symbols": total,
        "direction": direction,
        "tf": tf,
    }
