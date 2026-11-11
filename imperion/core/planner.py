"""Trade planning: playbook + price-action map -> executable plan (entry, stop, TP1, TP2) or a clear rejection.

  stop     the playbook's invalidation plus a buffer; never closer than MIN_STOP_ATR, never wider than the style
           allows (the minor structure of the last candles is tried before giving up).
  targets  liquidity, not fixed multiples: TP2 is the nearest pool / structural target that pays at least the style's
           minimum R (capped at its maximum and at a range edge for range plays). TP1 sits before the first obstacle.
  filters  room to the first obstacle, and costs (fees + slippage) relative to the risk.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..analysis.priceaction.types import PAMap, Playbook
from ..config import Settings, StyleConfig
from ..models import TradeSetup
from ..util import clamp

MIN_STOP_ATR = 0.5
STOP_BUFFER_ATR = 0.20
MIN_ROOM_R = 1.0
TP1_MIN_R, TP1_MAX_R = 1.0, 1.5


def round_trip_cost_pct(cfg: Settings) -> float:
    return 2 * cfg.taker_fee_pct + 2 * cfg.slippage_pct


def build_plan(symbol: str, style: StyleConfig, pb: Playbook, m: PAMap, cfg: Settings,
               price: Optional[float] = None) -> Tuple[Optional[TradeSetup], List[str], Dict[str, Any]]:
    """`price` is the latest traded price (entry); the setup map supplies ATR, liquidity and structure."""
    facts: Dict[str, Any] = {}
    price, a = (price if price and price > 0 else m.price), m.atr
    if not m.ok or price <= 0 or a <= 0:
        return None, ["PLAN_NO_PRICE_OR_ATR"], facts
    long_ = pb.side == "LONG"
    sign = 1 if long_ else -1
    inv = pb.invalidation
    if inv is None or (long_ and inv >= price) or ((not long_) and inv <= price):
        return None, ["INVALIDATED (price is already beyond the playbook invalidation)"], facts
    notes = [f"playbook {pb.name}"]
    if pb.name in ("ote_pullback", "mitigation_retest"):
        candidates = [z for z in m.zones if z.direction == ("BULLISH" if long_ else "BEARISH")
                      and z.mean_threshold is not None]
        if candidates:
            ref = min(candidates, key=lambda z: abs(z.mean_threshold - price))
            facts["equilibrium_reference"] = ref.mean_threshold
            facts["equilibrium_distance_atr"] = round(abs(ref.mean_threshold - price) / a, 3)
            notes.append(f"zone equilibrium (50%) reference: {ref.mean_threshold:.8g}; market entry remains current executable price")
    stop = inv - STOP_BUFFER_ATR * a if long_ else inv + STOP_BUFFER_ATR * a
    dist = abs(price - stop)
    if dist < MIN_STOP_ATR * a:
        stop, dist = price - sign * MIN_STOP_ATR * a, MIN_STOP_ATR * a
        notes.append("stop widened to the minimum distance")
    if dist > style.max_stop_atr * a:
        minor = m.recent_low if long_ else m.recent_high
        ok = minor is not None and (minor < price - MIN_STOP_ATR * a if long_ else minor > price + MIN_STOP_ATR * a)
        alt = (minor - STOP_BUFFER_ATR * a if long_ else minor + STOP_BUFFER_ATR * a) if ok else None
        if alt is None or abs(price - alt) > style.max_stop_atr * a:
            return None, [f"STOP_TOO_WIDE ({dist / a:.1f} ATR > {style.max_stop_atr})"], facts
        stop, dist = alt, abs(price - alt)
        notes.append("minor-structure stop")

    def r_of(x: float) -> float:
        return (x - price) / dist if long_ else (price - x) / dist

    pools = [p.price for p in (m.pools_above if long_ else m.pools_below)]
    obstacles = sorted(r for r in (r_of(x) for x in pools) if r > 0.05)
    first = obstacles[0] if obstacles else None
    facts["first_obstacle_r"] = round(first, 2) if first is not None else None
    if first is not None and first < MIN_ROOM_R:
        return None, [f"NO_ROOM (liquidity {first:.2f}R away < {MIN_ROOM_R}R)"], facts
    targets = sorted({r for r in obstacles + ([r_of(pb.target)] if pb.target is not None else []) if r >= style.min_rr})
    tp2_r = targets[0] if targets else style.min_rr
    tp2_r = min(tp2_r, style.max_rr)
    if pb.cap is not None:
        cap_r = r_of(pb.cap) * 0.97
        if cap_r < style.min_rr:
            return None, [f"RANGE_TOO_SMALL (edge {cap_r:.2f}R < {style.min_rr}R)"], facts
        tp2_r = min(tp2_r, cap_r)
    tp1_r = clamp(first * 0.95 if first is not None else 1.2, TP1_MIN_R, min(TP1_MAX_R, tp2_r * 0.7))
    facts.update(tp1_r=round(tp1_r, 2), tp2_r=round(tp2_r, 2), obstacle_before_tp2=bool(first is not None and first < tp2_r - 1e-9))
    if facts["obstacle_before_tp2"]:
        notes.append(f"liquidity at {first:.1f}R before the final target")
    stop_pct = dist / price * 100
    cost = round_trip_cost_pct(cfg)
    if cost / stop_pct > cfg.max_cost_to_risk:
        return None, [f"COST_EXCEEDS_EDGE (fees+slippage {cost:.2f}% = {cost / stop_pct:.0%} of a {stop_pct:.2f}% stop; max {cfg.max_cost_to_risk:.0%})"], facts
    tp1, tp2 = price + sign * tp1_r * dist, price + sign * tp2_r * dist
    return TradeSetup(symbol, style.name, pb.side, price, stop, tp1, tp2, a, tp2_r, stop_pct, notes, playbook=pb.name), [], facts


def quality(pb_score: float, net: float, n_agree: int, alignment: float, rr: float, style: StyleConfig, health: float,
            flow_confirmed: bool, obstacle_before_tp2: bool) -> float:
    """0..100. Grade thresholds: A+ >= 85, A >= 77, B >= style.min_quality."""
    q = 100 * (0.30 * pb_score + 0.25 * min(1.0, abs(net) / 0.5) + 0.15 * min(1.0, n_agree / 7) + 0.15 * alignment
               + 0.10 * min(1.0, rr / (style.min_rr * 1.5)) + 0.05 * health)
    if not flow_confirmed:
        q -= 5
    if obstacle_before_tp2:
        q -= 4
    return round(max(0.0, min(100.0, q)), 1)


def grade(q: float, style: StyleConfig) -> str:
    if q >= 85:
        return "A+"
    if q >= 77:
        return "A"
    if q >= style.min_quality:
        return "B"
    return "C"
