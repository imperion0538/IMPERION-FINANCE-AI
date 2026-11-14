
"""IMPERION FINANCE AI V17 - Advanced Trade Planning Engine.

Converts a validated Price Action playbook into an executable
trade plan or a documented rejection.

Preserves public V17 interfaces:
    round_trip_cost_pct(cfg)
    build_plan(symbol, style, pb, m, cfg, price=None)
    quality(...)
    grade(...)

Safety principles:
- Reject non-finite prices and risk parameters.
- Keep stops beyond playbook invalidation.
- Enforce ATR stop-distance limits.
- Respect directional liquidity obstacles and range caps.
- Reject trades with insufficient room or excessive costs.
- Never place orders from this module.

A plan is a proposal, not permission to execute. Portfolio risk
limits, exchange filters and execution guards remain mandatory.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from ..analysis.priceaction.types import PAMap, Playbook
from ..config import Settings, StyleConfig
from ..models import TradeSetup
from ..util import clamp


MIN_STOP_ATR = 0.50
STOP_BUFFER_ATR = 0.20
MIN_ROOM_R = 1.00

TP1_MIN_R = 1.00
TP1_MAX_R = 1.50

EPSILON = 1e-12


def _finite(value: Any) -> bool:
    """Reject booleans, non-numeric and non-finite values."""
    if isinstance(value, bool):
        return False

    if not isinstance(value, (int, float)):
        return False

    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _positive(value: Any) -> bool:
    return _finite(value) and value > 0


def _reject(
    reason: str,
    facts: Dict[str, Any],
) -> Tuple[None, List[str], Dict[str, Any]]:
    """Return the existing planner rejection structure."""
    facts["rejection"] = reason
    return None, [reason], facts


def round_trip_cost_pct(cfg: Settings) -> float:
    """Estimated round-trip taker fees and slippage, in percent."""
    fee = cfg.taker_fee_pct
    slippage = cfg.slippage_pct

    if (
        not _finite(fee)
        or not _finite(slippage)
        or fee < 0
        or slippage < 0
    ):
        return float("nan")

    return 2.0 * (fee + slippage)


def build_plan(
    symbol: str,
    style: StyleConfig,
    pb: Playbook,
    m: PAMap,
    cfg: Settings,
    price: Optional[float] = None,
) -> Tuple[
    Optional[TradeSetup],
    List[str],
    Dict[str, Any],
]:
    """Build a directional plan with bounded risk geometry.

    The current executable price is preferred when explicitly
    supplied. Missing or invalid explicit prices are rejected
    rather than silently replaced by an older map price.
    """
    facts: Dict[str, Any] = {}

    if not m.ok:
        return _reject("PLAN_MAP_NOT_READY", facts)

    entry = m.price if price is None else price
    atr = m.atr

    if not _positive(entry) or not _positive(atr):
        return _reject("PLAN_NO_PRICE_OR_ATR", facts)

    if pb.side not in ("LONG", "SHORT"):
        return _reject("PLAN_INVALID_SIDE", facts)

    if not _finite(pb.score) or not 0 <= pb.score <= 1:
        return _reject("PLAN_INVALID_PLAYBOOK_SCORE", facts)

    if not _positive(pb.invalidation):
        return _reject("PLAN_INVALID_INVALIDATION", facts)

    min_rr = style.min_rr
    max_rr = style.max_rr
    max_stop_atr = style.max_stop_atr

    if not all(
        _positive(value)
        for value in (min_rr, max_rr, max_stop_atr)
    ):
        return _reject("PLAN_INVALID_STYLE_LIMITS", facts)

    if (
        max_rr < min_rr
        or max_stop_atr < MIN_STOP_ATR
    ):
        return _reject("PLAN_INCONSISTENT_STYLE_LIMITS", facts)

    max_cost_to_risk = cfg.max_cost_to_risk

    if (
        not _finite(max_cost_to_risk)
        or not 0 <= max_cost_to_risk <= 1
    ):
        return _reject("PLAN_INVALID_COST_LIMIT", facts)

    long_ = pb.side == "LONG"
    sign = 1 if long_ else -1
    invalidation = pb.invalidation

    if (
        (long_ and invalidation >= entry)
        or (not long_ and invalidation <= entry)
    ):
        return _reject("PLAN_ALREADY_INVALIDATED", facts)

    if pb.target is not None:
        if not _positive(pb.target):
            return _reject("PLAN_INVALID_TARGET", facts)

        if (
            (long_ and pb.target <= entry)
            or (not long_ and pb.target >= entry)
        ):
            return _reject("PLAN_TARGET_WRONG_SIDE", facts)

    if pb.cap is not None:
        if not _positive(pb.cap):
            return _reject("PLAN_INVALID_CAP", facts)

        if (
            (long_ and pb.cap <= entry)
            or (not long_ and pb.cap >= entry)
        ):
            return _reject("PLAN_CAP_WRONG_SIDE", facts)

        if pb.target is not None:
            if (
                (long_ and pb.target > pb.cap)
                or (not long_ and pb.target < pb.cap)
            ):
                return _reject("PLAN_TARGET_EXCEEDS_CAP", facts)

    notes = [f"playbook {pb.name}"]

    if pb.name in ("ote_pullback", "mitigation_retest"):
        expected_direction = (
            "BULLISH" if long_ else "BEARISH"
        )

        candidates = [
            zone
            for zone in m.zones
            if zone.direction == expected_direction
            and _positive(zone.mean_threshold)
        ]

        if candidates:
            reference = min(
                candidates,
                key=lambda zone: abs(
                    zone.mean_threshold - entry
                ),
            )
            equilibrium = reference.mean_threshold

            facts["equilibrium_reference"] = equilibrium
            facts["equilibrium_distance_atr"] = round(
                abs(equilibrium - entry) / atr,
                3,
            )

            notes.append(
                "zone equilibrium (50%) reference: "
                f"{equilibrium:.8g}; market entry remains "
                "current executable price"
            )

    # The original playbook invalidation is authoritative.
    # A minimum stop distance may widen it, never tighten it.
    stop = (
        invalidation - STOP_BUFFER_ATR * atr
        if long_
        else invalidation + STOP_BUFFER_ATR * atr
    )

    distance = abs(entry - stop)
    minimum_distance = MIN_STOP_ATR * atr
    maximum_distance = max_stop_atr * atr

    if not _positive(distance):
        return _reject("PLAN_INVALID_STOP_DISTANCE", facts)

    if distance < minimum_distance:
        stop = entry - sign * minimum_distance
        distance = minimum_distance
        notes.append(
            "stop widened to the minimum ATR distance"
        )

    # Never move the stop inside the original invalidation.
    if distance > maximum_distance + EPSILON:
        facts["stop_distance_atr"] = round(
            distance / atr,
            3,
        )
        return _reject("STOP_TOO_WIDE", facts)

    if (
        (long_ and stop >= invalidation)
        or (not long_ and stop <= invalidation)
    ):
        return _reject("PLAN_STOP_VIOLATES_INVALIDATION", facts)

    if not _positive(stop):
        return _reject("PLAN_INVALID_STOP_PRICE", facts)

    def r_of(level: float) -> float:
        return sign * (level - entry) / distance

    # Only forward, usable liquidity levels are obstacles.
    pools = m.pools_above if long_ else m.pools_below
    obstacle_levels = []

    for pool in pools:
        level = pool.price

        if not _positive(level):
            continue

        if getattr(pool, "state", "FRESH") not in (
            "FRESH",
            "TESTED",
        ):
            continue

        distance_r = r_of(level)

        if _finite(distance_r) and distance_r > 0.05:
            obstacle_levels.append(distance_r)

    obstacles = sorted(obstacle_levels)
    first = obstacles[0] if obstacles else None

    facts["first_obstacle_r"] = (
        round(first, 3) if first is not None else None
    )

    if first is not None and first < MIN_ROOM_R:
        return _reject("PLAN_INSUFFICIENT_LIQUIDITY_ROOM", facts)

    # Candidate targets must lie in the correct direction.
    # Do not invent a TP2 when no structural target exists.
    targets = list(obstacles)

    if pb.target is not None:
        targets.append(r_of(pb.target))

    eligible_targets = sorted(
        {
            value
            for value in targets
            if _finite(value)
            and min_rr <= value <= max_rr
        }
    )

    if not eligible_targets:
        return _reject("PLAN_NO_ELIGIBLE_TARGET", facts)

    tp2_r = eligible_targets[0]

    if pb.cap is not None:
        cap_r = r_of(pb.cap) * 0.97

        if not _finite(cap_r) or cap_r < min_rr:
            facts["cap_r"] = (
                round(cap_r, 3)
                if _finite(cap_r)
                else None
            )
            return _reject("PLAN_RANGE_TOO_SMALL", facts)

        tp2_r = min(tp2_r, cap_r)

    if tp2_r < min_rr:
        return _reject("PLAN_TP2_BELOW_MIN_RR", facts)

    # TP1 must be ahead of entry and strictly before TP2.
    tp1_ceiling = min(
        TP1_MAX_R,
        tp2_r * 0.70,
    )

    if tp1_ceiling <= 0:
        return _reject("PLAN_NO_VALID_TP1", facts)

    preferred_tp1 = (
        first * 0.95 if first is not None else 1.20
    )

    tp1_floor = min(TP1_MIN_R, tp1_ceiling)

    tp1_r = clamp(
        preferred_tp1,
        tp1_floor,
        tp1_ceiling,
    )

    if not 0 < tp1_r < tp2_r:
        return _reject("PLAN_INVALID_TARGET_ORDER", facts)

    obstacle_before_tp2 = (
        first is not None
        and first < tp2_r - EPSILON
    )

    facts.update(
        entry=entry,
        atr=atr,
        invalidation=invalidation,
        stop=stop,
        stop_distance_atr=round(distance / atr, 3),
        tp1_r=round(tp1_r, 3),
        tp2_r=round(tp2_r, 3),
        obstacle_before_tp2=obstacle_before_tp2,
    )

    if obstacle_before_tp2:
        notes.append(
            f"liquidity at {first:.2f}R before final target"
        )

    stop_pct = distance / entry * 100.0
    cost_pct = round_trip_cost_pct(cfg)

    if not _positive(stop_pct) or not _finite(cost_pct):
        return _reject("PLAN_INVALID_COST_OR_STOP", facts)

    cost_to_risk = cost_pct / stop_pct

    facts["round_trip_cost_pct"] = round(cost_pct, 6)
    facts["cost_to_risk"] = round(cost_to_risk, 4)

    if cost_to_risk > max_cost_to_risk:
        return _reject("PLAN_COST_EXCEEDS_EDGE", facts)

    tp1 = entry + sign * tp1_r * distance
    tp2 = entry + sign * tp2_r * distance

    if not all(
        _positive(value)
        for value in (stop, tp1, tp2)
    ):
        return _reject("PLAN_NON_FINITE_LEVEL", facts)

    if long_:
        valid_order = stop < entry < tp1 < tp2
    else:
        valid_order = tp2 < tp1 < entry < stop

    if not valid_order:
        return _reject("PLAN_INVALID_PRICE_GEOMETRY", facts)

    # VP reference levels are advisory; V17's validated executable market entry,
    # invalidation stop, target geometry and capital guard remain authoritative.
    vp_candidates = [r for r in m.vp_ob_confluence
                     if r.get("direction") == ("BULLISH" if long_ else "BEARISH")
                     and r.get("preferred") is True
                     and r.get("distance_atr", float("inf")) <= 1.5]
    if vp_candidates and m.volume_profile:
        from ..analysis.priceaction.volume_profile import (
            VolumeProfile, refine_entry_stop_with_vp,
        )
        data = m.volume_profile
        vp = VolumeProfile(poc=data["poc"], vah=data["vah"],
                           val=data["val"], total_volume=data["volume"],
                           hvn_levels=list(data.get("hvn") or []),
                           lvn_levels=list(data.get("lvn") or []))
        ref_entry, ref_stop, ref_note = refine_entry_stop_with_vp(
            "BULLISH" if long_ else "BEARISH", entry, stop, vp, atr,
        )
        best = max(vp_candidates, key=lambda r: r["vp_score"])
        facts["vp_ob_reference"] = {
            "score": best["vp_score"], "reasons": best["reasons"],
            "entry": ref_entry, "stop": ref_stop,
            "note": ref_note, "executed": False,
        }
        notes.append(
            "OB+VP evidence (OHLCV estimate); alternative entry/stop "
            "is advisory only; V17 validated prices preserved"
        )

    facts["tp1"] = tp1
    facts["tp2"] = tp2
    facts["validated"] = True

    trade = TradeSetup(
        symbol,
        style.name,
        pb.side,
        entry,
        stop,
        tp1,
        tp2,
        atr,
        tp2_r,
        stop_pct,
        notes,
        playbook=pb.name,
    )

    return trade, [], facts


def quality(
    pb_score: float,
    net: float,
    n_agree: int,
    alignment: float,
    rr: float,
    style: StyleConfig,
    health: float,
    flow_confirmed: bool,
    obstacle_before_tp2: bool,
) -> float:
    """Calculate a bounded 0..100 setup-quality score.

    This score is not a calibrated probability of success.
    """
    values = (
        pb_score,
        net,
        n_agree,
        alignment,
        rr,
        health,
        style.min_rr,
    )

    if not all(_finite(value) for value in values):
        return 0.0

    if (
        not 0 <= pb_score <= 1
        or not 0 <= alignment <= 1
        or not 0 <= health <= 1
        or n_agree < 0
        or rr <= 0
        or style.min_rr <= 0
    ):
        return 0.0

    score = 100.0 * (
        0.30 * pb_score
        + 0.25 * min(1.0, abs(net) / 0.5)
        + 0.15 * min(1.0, n_agree / 7)
        + 0.15 * alignment
        + 0.10 * min(
            1.0,
            rr / (style.min_rr * 1.5),
        )
        + 0.05 * health
    )

    if not flow_confirmed:
        score -= 5.0

    if obstacle_before_tp2:
        score -= 4.0

    return round(clamp(score, 0.0, 100.0), 1)


def grade(
    q: float,
    style: StyleConfig,
) -> str:
    """Return the existing V17 A+/A/B/C grade."""
    if not _finite(q) or not 0 <= q <= 100:
        return "C"

    if q >= 85:
        return "A+"

    if q >= 77:
        return "A"

    if q >= style.min_quality:
        return "B"

    return "C"
