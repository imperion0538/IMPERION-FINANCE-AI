"""Tiered Radar candidate analysis; this module never sends orders.

Tier 1 scores candidates using 1h data. Tier 2 selects a bounded subset for
Director analysis. Runtime admission remains a separate opt-in gate.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .indicators import atr
from .mtf import cached_tf_bias

RADAR_TF = "1h"
DEEP_TFS = ("5m", "15m", "1h", "4h")
DEEP_STYLE = "day"
MIN_HISTORY = 100
ATR_BAND = (0.25, 5.0)          # % of price per 1h candle: dead markets and blow-offs are skipped


def tier1(candidates: Sequence[Dict[str, Any]], candles) -> List[Dict[str, Any]]:
    """Score every candidate; best first. Pure function of the radar list and the 1h candles."""
    n = max(1, len(candidates))
    by_volume = sorted(candidates, key=lambda m: -float(m.get("quote_volume") or 0))
    vol_rank = {m["symbol"]: i for i, m in enumerate(by_volume)}
    out = []
    for m in candidates:
        sym = m["symbol"]
        cs = candles.get(sym, RADAR_TF)
        bias = cached_tf_bias(candles, sym, RADAR_TF)
        a = atr(cs) if len(cs) >= 20 else None
        atr_pct = (a / cs[-1].c * 100) if a and cs and cs[-1].c > 0 else None
        reasons = []
        if len(cs) < MIN_HISTORY:
            reasons.append(f"history {len(cs)} < {MIN_HISTORY}")
        if atr_pct is None or not ATR_BAND[0] <= atr_pct <= ATR_BAND[1]:
            reasons.append("volatility outside band" if atr_pct is not None else "no ATR")
        if bias["bias"] == "UNKNOWN":
            reasons.append("no 1h structure")
        score = (0.45 * min(1.0, abs(bias["score"])) + 0.35 * (1 - vol_rank[sym] / n)
                 + 0.20 * min(1.0, abs(float(m.get("price_change_pct") or 0)) / 8.0))
        out.append({"symbol": sym, "eligible": not reasons, "score": round(score, 3), "bias_1h": bias["bias"],
                    "atr_pct_1h": round(atr_pct, 3) if atr_pct is not None else None, "quote_volume": m.get("quote_volume"),
                    "price_change_pct": m.get("price_change_pct"), "why_not": reasons})
    return sorted(out, key=lambda r: (not r["eligible"], -r["score"]))


def select_deep(ranked: Sequence[Dict[str, Any]], count: int) -> List[str]:
    return [r["symbol"] for r in ranked if r["eligible"]][:max(0, count)]
