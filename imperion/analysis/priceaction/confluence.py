"""Evidence-aware confluence scoring for Advanced Market Intelligence.

Scores are setup-completeness modifiers, not win probabilities. Correlated evidence
is capped so SMT/session/flow confirmations cannot inflate a setup without bound.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from ...models import BEARISH, BULLISH, Candle
from .types import PAMap


def displacement_quality(candles: Sequence[Candle], atr: float, index: Optional[int] = None) -> Dict[str, float]:
    if not candles or atr <= 0:
        return {"score": 0.0, "body_ratio": 0.0, "atr_multiple": 0.0, "volume_ratio": 0.0, "continuity": 0.0}
    i = len(candles) - 1 if index is None else max(0, min(index, len(candles) - 1))
    c = candles[i]
    body_ratio = c.body / max(c.range, 1e-12)
    atr_multiple = c.body / atr
    hist = list(candles[max(0, i - 20):i])
    avg_vol = sum(x.v for x in hist) / len(hist) if hist else c.v
    volume_ratio = c.v / max(avg_vol, 1e-12) if avg_vol > 0 else 1.0
    same_direction = 0
    for prev in reversed(candles[max(0, i - 3):i]):
        if prev.bullish == c.bullish and prev.body / max(prev.range, 1e-12) >= 0.45:
            same_direction += 1
        else:
            break
    continuity = min(1.0, same_direction / 3.0)
    score = (0.30 * min(1.0, body_ratio / 0.75)
             + 0.30 * min(1.0, atr_multiple / 1.8)
             + 0.20 * min(1.0, volume_ratio / 1.8)
             + 0.20 * continuity)
    return {"score": round(max(0.0, min(1.0, score)), 3),
            "body_ratio": round(body_ratio, 3), "atr_multiple": round(atr_multiple, 3),
            "volume_ratio": round(volume_ratio, 3), "continuity": round(continuity, 3)}


def triple_confluence(map_: PAMap, direction: str, *, min_idm_quality: float = 0.62, tolerance_atr: float = 0.35) -> Dict[str, Any]:
    """OTE + IDM + mitigation proximity in the same direction and price neighborhood."""
    if not map_.ok or map_.range is None or not map_.range.ote_zone:
        return {"active": False, "score": 0.0, "reasons": []}
    idms = [x for x in map_.inducements if x.direction == direction and x.quality >= min_idm_quality and x.age <= 18]
    mits = [z for z in map_.mitigation_zones if z.direction == direction and z.quality >= min_idm_quality
            and z.lo - tolerance_atr * map_.atr <= map_.price <= z.hi + tolerance_atr * map_.atr]
    if not idms or not mits:
        return {"active": False, "score": 0.0, "reasons": []}
    # OTE is already true above. Require IDM and mitigation to be close enough
    # to the current dealing-range midpoint to represent one coherent setup.
    midpoint = (map_.range.high + map_.range.low) / 2.0
    idm = min(idms, key=lambda x: abs(x.level - midpoint))
    mit = min(mits, key=lambda x: abs((x.lo + x.hi) / 2.0 - map_.price))
    coherent = abs(idm.level - (mit.lo + mit.hi) / 2.0) <= 1.0 * map_.atr
    active = coherent
    return {"active": active, "score": 0.15 if active else 0.0,
            "reasons": ["OTE aktif", f"IDM kalite={idm.quality:.2f}",
                        f"Mitigation kalite={mit.quality:.2f}"] if active else []}


def smt_modifier(smt: Optional[dict], direction: str) -> tuple[float, str]:
    if not smt or not smt.get("available"):
        return 0.0, "SMT kullanılamıyor; eşleşmiş veri yok"
    smt_dir = smt.get("direction")
    if smt_dir == direction:
        return 0.10, f"SMT teyidi: {direction} ({smt.get('comparison_symbol', 'eş varlık')})"
    if smt_dir in (BULLISH, BEARISH) and smt_dir != direction:
        return -0.08, f"SMT karşı yönde: {smt_dir}"
    if smt_dir == "CONFLICT":
        return -0.03, "SMT sinyalleri çelişkili"
    return 0.0, "SMT nötr"


def adaptive_thresholds(volatility: str, base: Optional[dict] = None) -> Dict[str, float]:
    """Bounded regime adjustments; never loosens risk gates or mutates global config."""
    values = dict(base or {"idm_min_quality": 0.62, "displacement_min": 0.64, "ote_tolerance_atr": 0.35})
    regime = (volatility or "NORMAL").upper()
    if regime in ("HIGH", "VOLATILE", "EXTREME"):
        values["idm_min_quality"] = min(0.90, values["idm_min_quality"] + 0.06)
        values["displacement_min"] = min(0.90, values["displacement_min"] + 0.08)
        values["ote_tolerance_atr"] = max(0.15, values["ote_tolerance_atr"] * 0.80)
    elif regime in ("LOW", "QUIET"):
        # Low volatility does not relax entry quality; it only narrows zone tolerance.
        values["ote_tolerance_atr"] = max(0.15, values["ote_tolerance_atr"] * 0.90)
    return {k: round(float(v), 4) for k, v in values.items()}


def flow_modifier(flow: Optional[dict], direction: str) -> tuple[float, str]:
    """Small bounded confirmation from live AggTrade CVD/delta; unavailable is neutral."""
    if not flow or not flow.get("available"):
        return 0.0, "Order Flow doğrulaması mevcut değil"
    delta = float(flow.get("delta_ratio") or 0.0)
    aligned = delta > 0.08 if direction == BULLISH else delta < -0.08
    opposed = delta < -0.12 if direction == BULLISH else delta > 0.12
    if aligned:
        return 0.06, f"AggTrade CVD/Delta yönü destekliyor (delta={delta:+.3f})"
    if opposed:
        return -0.06, f"AggTrade CVD/Delta karşı yönde (delta={delta:+.3f})"
    return 0.0, f"AggTrade Delta nötr (delta={delta:+.3f})"


def candle_flow_divergence(candles: Sequence[Candle], lookback: int = 16) -> Dict[str, Any]:
    """Approximate price-vs-aggressor-delta divergence from candle taker-buy volume.

    This is a candle-level proxy, not exchange tick CVD. It is reported separately
    from the live AggTrade profile to avoid conflating two data resolutions.
    """
    if len(candles) < lookback or lookback < 8:
        return {"available": False, "direction": "NONE", "source": "candle_delta_proxy"}
    cs = list(candles[-lookback:])
    mid = len(cs) // 2
    left, right = cs[:mid], cs[mid:]
    left_cvd, right_cvd = [], []
    acc = 0.0
    for c in left:
        acc += c.delta
        left_cvd.append(acc)
    left_end = acc
    acc = left_end
    for c in right:
        acc += c.delta
        right_cvd.append(acc)
    bull = min(c.l for c in right) < min(c.l for c in left) and min(right_cvd) > min(left_cvd)
    bear = max(c.h for c in right) > max(c.h for c in left) and max(right_cvd) < max(left_cvd)
    direction = BULLISH if bull and not bear else BEARISH if bear and not bull else "CONFLICT" if bull and bear else "NONE"
    return {"available": True, "direction": direction, "source": "candle_delta_proxy",
            "price_lower_low": bool(bull), "price_higher_high": bool(bear)}
