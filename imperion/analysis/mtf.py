"""Multi-timeframe view: structure + EMA trend on each timeframe and the weighted agreement between them."""
from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, Sequence, Tuple

from ..models import BEARISH, BULLISH, NEUTRAL, Candle
from .indicators import atr_series, ema
from .priceaction.structure import analyze_structure, find_swings

TF_WEIGHT = {"1m": 0.5, "5m": 0.7, "15m": 0.9, "30m": 1.0, "1h": 1.2, "4h": 1.4, "1d": 1.5, "1w": 1.2}
_CACHE: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
_LOCK = threading.Lock()
_CACHE_MAX = 4096


def _sign(d: str) -> int:
    return 1 if d == BULLISH else -1 if d == BEARISH else 0


def tf_bias(cs: Sequence[Candle]) -> Dict[str, Any]:
    """Structure (BOS/CHoCH trend) + EMA 20/50/200 stack of one series -> bias and a signed score in [-1, 1]."""
    out: Dict[str, Any] = {"structure": "UNKNOWN", "trend": "UNKNOWN", "bias": "UNKNOWN", "score": 0.0}
    if len(cs) < 40:
        return out
    cs = list(cs[-300:])
    atrs = atr_series(cs)
    st = analyze_structure(cs, find_swings(cs), atrs) if len(atrs) == len(cs) else None
    s_dir = {"UP": BULLISH, "DOWN": BEARISH, "RANGE": NEUTRAL}.get(st.trend if st else "", "UNKNOWN")
    closes = [c.c for c in cs]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    t_dir, t_score = "UNKNOWN", 0.0
    if e20 is not None and e50 is not None:
        px = closes[-1]
        if e200 is not None and px > e20 > e50 > e200:
            t_dir, t_score = BULLISH, 1.0
        elif e200 is not None and px < e20 < e50 < e200:
            t_dir, t_score = BEARISH, -1.0
        elif px > e50 and e20 > e50:
            t_dir, t_score = BULLISH, 0.5
        elif px < e50 and e20 < e50:
            t_dir, t_score = BEARISH, -0.5
        else:
            t_dir = NEUTRAL
    s_score = 0.0
    if st is not None and s_dir in (BULLISH, BEARISH):
        s_score = _sign(s_dir) * (1.0 if st.last_break and st.last_break.kind == "BOS" else 0.7)
    known = [x for x in (s_dir, t_dir) if x != "UNKNOWN"]
    score = (0.6 * s_score + 0.4 * t_score) if known else 0.0
    bias = "UNKNOWN" if not known else BULLISH if score > 0.15 else BEARISH if score < -0.15 else NEUTRAL
    out.update(structure=s_dir, trend=t_dir, bias=bias, score=round(score, 3))
    return out


def cached_tf_bias(candles, symbol: str, tf: str) -> Dict[str, Any]:
    cs = candles.get(symbol, tf)
    # first/last open time, length and the last OHLC identify the series content (safe across different data sets)
    key = (symbol, tf, len(cs), cs[0].t, cs[-1].t, cs[-1].o, cs[-1].h, cs[-1].l, cs[-1].c) if cs else (symbol, tf, 0)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    res = tf_bias(cs)
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = res
    return res


def mtf_view(candles, symbol: str, tfs: Iterable[str]) -> Dict[str, Any]:
    out: Dict[str, Dict[str, Any]] = {}
    signed = total = 0.0
    for tf in tfs:
        b = cached_tf_bias(candles, symbol, tf)
        out[tf] = b
        if b["bias"] != "UNKNOWN":
            w = TF_WEIGHT.get(tf, 1.0)
            total += w
            signed += w * b["score"]
    net = signed / total if total else 0.0
    return {"tfs": out, "net": round(net, 3), "confluence": round(min(1.0, abs(net)), 3),
            "direction": BULLISH if net > 0.15 else BEARISH if net < -0.15 else NEUTRAL}


def alignment(mtf: Dict[str, Any], direction: str) -> float:
    """Share (0..1) of timeframe weight that agrees with `direction`."""
    tot = agree = 0.0
    for tf, b in (mtf.get("tfs") or {}).items():
        if b.get("bias") in (BULLISH, BEARISH, NEUTRAL):
            w = TF_WEIGHT.get(tf, 1.0)
            tot += w
            agree += w if b["bias"] == direction else 0.0
    return agree / tot if tot else 0.0
