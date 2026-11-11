"""Order flow: who is aggressive, and is it working?

Candle flow (any timeframe, from REST taker-buy volume): delta, CVD slope, CVD/price divergence and absorption.
Live flow (WebSocket): order-book imbalance, whale prints and footprint stacked imbalances.
Both readings return a signed score in [-1, 1] (positive = buyers in control) plus their components.
"""
from __future__ import annotations

from statistics import median
from typing import Any, Dict, Optional, Sequence

from ..models import Candle
from ..util import clamp, now_ms
from .indicators import atr
from .priceaction.structure import find_swings

MIN_CANDLES = 60
WARMUP_MINUTES = 5


def candle_flow(cs: Sequence[Candle]) -> Dict[str, Any]:
    if len(cs) < MIN_CANDLES:
        return {"available": False}
    a = atr(cs) or 0.0
    last3 = cs[-3:]
    v3 = sum(c.v for c in last3)
    delta3 = sum(c.delta for c in last3) / v3 if v3 > 0 else 0.0
    window = cs[-20:]
    avg_v = sum(c.v for c in cs[-60:]) / 60 or 1e-12
    cvd_slope = sum(c.delta for c in window) / (avg_v * len(window))
    price_up = window[-1].c > window[0].c
    # divergence between the last two swings
    seg = list(cs[-70:])
    cum, run = [], 0.0
    for c in seg:
        run += c.delta
        cum.append(run)
    sw = find_swings(seg, 3, 3)
    highs, lows = [s for s in sw if s.kind == "H"][-2:], [s for s in sw if s.kind == "L"][-2:]
    scale = (sum(c.v for c in seg) / len(seg)) or 1e-12
    divergence = 0
    if len(highs) == 2 and len(seg) - 1 - highs[1].i <= 18 and highs[1].price > highs[0].price and cum[highs[1].i] < cum[highs[0].i] - 0.5 * scale:
        divergence = -1
    if len(lows) == 2 and len(seg) - 1 - lows[1].i <= 18 and lows[1].price < lows[0].price and cum[lows[1].i] > cum[lows[0].i] + 0.5 * scale:
        divergence = 1
    # absorption: heavy aggression that fails to move price
    absorption = 0
    avg30 = sum(c.v for c in cs[-31:-1]) / 30
    for c in (cs[-1], cs[-2]):
        if c.v <= 0 or avg30 <= 0 or a <= 0:
            continue
        ratio = c.delta / c.v
        if c.v >= 1.5 * avg30 and abs(ratio) >= 0.25 and (c.body <= 0.35 * max(c.range, 1e-12) or c.range <= 0.8 * a):
            absorption = 1 if ratio < 0 else -1          # sellers hit and price held -> buyers absorbed
            break
    score = (0.35 * clamp(delta3 / 0.25, -1, 1) + 0.30 * clamp(cvd_slope / 0.3, -1, 1) * (1.0 if (cvd_slope > 0) == price_up else 0.6)
             + 0.20 * divergence + 0.15 * absorption)
    return {"available": True, "score": round(clamp(score, -1, 1), 3), "delta3": round(delta3, 3), "cvd_slope": round(cvd_slope, 3),
            "divergence": divergence, "absorption": absorption}


def live_flow(trades, books, symbol: str, now: Optional[int] = None) -> Dict[str, Any]:
    """Order book + whale prints + footprint. Unavailable until the streams are warm."""
    now = now if now is not None else now_ms()
    parts: Dict[str, Any] = {}
    score, weight = 0.0, 0.0
    snap = books.get(symbol)
    if snap and now - snap["rx"] <= 30_000:
        bids, asks = snap["bids"][:10], snap["asks"][:10]
        bn, an = sum(p * q for p, q in bids), sum(p * q for p, q in asks)
        if bn + an > 0:
            imb = (bn - an) / (bn + an)
            mid = (bids[0][0] + asks[0][0]) / 2
            parts["book_imbalance"] = round(imb, 3)
            parts["spread_pct"] = round((asks[0][0] - bids[0][0]) / mid * 100, 4)
            notionals = [p * q for p, q in bids + asks]
            med = median(notionals) if notionals else 0
            parts["walls"] = sum(1 for x in notionals if med > 0 and x >= 4 * med)
            if abs(imb) >= 0.18:
                score += 0.35 * clamp(imb / 0.5, -1, 1)
            weight += 0.35
    if trades.coverage_minutes(symbol) >= WARMUP_MINUTES:
        w = trades.whale_stats(symbol, 15 * 60_000, now)
        parts["whale_net_15m"] = round(w["net_usdt"], 2)
        tot = w["buy_usdt"] + w["sell_usdt"]
        if tot >= 2 * w["threshold_usdt"] and abs(w["ratio"]) >= 0.25:
            score += 0.30 * clamp(w["ratio"] / 0.6, -1, 1)
        weight += 0.30
        profile = trades.flow_profile(symbol, 5, now=now)
        if profile.get("available"):
            parts["trade_flow"] = {k: profile[k] for k in ("buy_qty", "sell_qty", "volume_qty", "cvd", "delta_ratio", "poc_bin")}
            score += 0.20 * clamp(float(profile["delta_ratio"]) / 0.25, -1, 1)
            weight += 0.20
        st = trades.stacked_imbalances(symbol, 5, now=now)
        diff = st["buy_stacks"] - st["sell_stacks"]
        parts["footprint"] = {"buy": st["buy_stacks"], "sell": st["sell_stacks"], "levels": st["levels"]}
        if diff:
            score += 0.15 * clamp(diff / 2, -1, 1)
        weight += 0.15
    if weight == 0:
        return {"available": False}
    return {"available": True, "score": round(clamp(score / weight, -1, 1), 3), "coverage": round(weight, 2), **parts}
