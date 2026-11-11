"""IMPERION FINANCE AI V17 — Playbook Engine.

Eight setup models with confluence and candidate validation.
Scores represent setup completeness, not win probability.
Final trade planning and execution remain separate.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from ...models import BEARISH, BULLISH, SIDE_OF
from ...util import clamp
from .types import PAMap, Playbook
from .zones import describe, zone_quality, zones_at_price


SWEEP_MAX_AGE_SETUP = 6
SWEEP_MAX_AGE_TRIGGER = 10
TRIGGER_BREAK_MAX_AGE = 12

PULLBACK_MAX_POS = 0.62
OTE_LO, OTE_HI = 0.21, 0.382

RETEST_MAX_AGE = 30
RANGE_EDGE = 0.18
MIN_RANGE_ATR = 3.0

SWEEP_CONF_MIN = 0.45
RETEST_CONF_MIN = 0.40
PULLBACK_CONF_MIN = 0.30


REGIME_FIT: Dict[str, Dict[str, float]] = {
    "TREND_UP": {
        "trend_pullback": 1.0,
        "breakout_retest": 1.0,
        "sweep_reversal": 0.9,
        "range_fade": 0.0,
        "inducement_sweep": 1.05,
        "ote_pullback": 1.1,
        "mitigation_retest": 1.0,
    },
    "TREND_DOWN": {
        "trend_pullback": 1.0,
        "breakout_retest": 1.0,
        "sweep_reversal": 0.9,
        "range_fade": 0.0,
        "inducement_sweep": 1.05,
        "ote_pullback": 1.1,
        "mitigation_retest": 1.0,
    },
    "RANGE": {
        "trend_pullback": 0.75,
        "breakout_retest": 0.8,
        "sweep_reversal": 1.0,
        "range_fade": 1.0,
        "inducement_sweep": 1.0,
        "ote_pullback": 0.8,
        "mitigation_retest": 0.9,
    },
    "TRANSITION": {
        "trend_pullback": 0.9,
        "breakout_retest": 0.9,
        "sweep_reversal": 1.0,
        "range_fade": 0.8,
        "inducement_sweep": 1.05,
        "ote_pullback": 0.95,
        "mitigation_retest": 1.0,
    },
    "VOLATILE": {
        "trend_pullback": 0.85,
        "breakout_retest": 0.8,
        "sweep_reversal": 0.85,
        "range_fade": 0.6,
        "inducement_sweep": 0.9,
        "ote_pullback": 0.85,
        "mitigation_retest": 0.9,
    },
}


def _p(params: Optional[Any], name: str, default):
    """Read an injected ParameterSet value."""
    if params is None:
        return default
    return getattr(params, name, default)


def _up(d: str) -> str:
    return "UP" if d == BULLISH else "DOWN"


def _finite(value) -> bool:
    """Accept finite numeric values, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def confirmation(
    setup: PAMap,
    trigger: PAMap,
    d: str,
    params: Optional[Any] = None,
) -> Tuple[float, List[str]]:
    """Directional structure, candle and displacement evidence."""
    max_age = _p(
        params, "trigger_break_max_age", TRIGGER_BREAK_MAX_AGE
    )
    score, steps = 0.0, []

    if trigger.ok:
        lb = trigger.structure.last_break

        if lb and 0 <= lb.age <= max_age and lb.direction == _up(d):
            score = max(score, 0.75 if lb.kind == "CHOCH" else 0.6)
            steps.append(
                f"tetik TF {lb.kind} "
                f"{'yukarı' if d == BULLISH else 'aşağı'} "
                f"({lb.age} mum önce)"
            )

        if trigger.candle.direction == d:
            score = min(
                1.0,
                max(score, trigger.candle.strength)
                + (0.15 if score else 0.0),
            )
            steps.append(
                "tetik mumu: " + ", ".join(trigger.candle.patterns)
            )

        if trigger.displacement and trigger.displacement[0] == d:
            score = min(
                1.0,
                max(score, 0.55) + (0.05 if score else 0.0),
            )
            steps.append(
                f"tetik TF yer değiştirme "
                f"{trigger.displacement[1]:.1f} ATR"
            )

    if setup.ok and setup.candle.direction == d:
        score = min(
            1.0,
            max(score, setup.candle.strength * 0.9)
            + (0.1 if score else 0.0),
        )
        steps.append(
            "kurulum mumu: " + ", ".join(setup.candle.patterns)
        )

    return score, steps


def _htf(htf: Optional[dict], d: str) -> float:
    """Signed higher-timeframe agreement strength."""
    if not htf or htf.get("direction") not in (BULLISH, BEARISH):
        return 0.0

    strength = htf.get("strength", 0.0)

    if not _finite(strength) or not 0 <= strength <= 1:
        return 0.0

    return strength if htf["direction"] == d else -strength


def _opposite_pool(m: PAMap, d: str) -> Optional[float]:
    """Nearest active liquidity target in the intended direction."""
    pools = m.pools_above if d == BULLISH else m.pools_below

    prices = [
        pool.price
        for pool in pools
        if _finite(pool.price)
        and pool.price > 0
        and getattr(pool, "state", "FRESH") in ("FRESH", "TESTED")
        and (
            pool.price > m.price
            if d == BULLISH
            else pool.price < m.price
        )
    ]

    if not prices:
        return None

    return min(prices) if d == BULLISH else max(prices)


def _range_target(m: PAMap, d: str) -> Optional[float]:
    if m.range is None:
        return None

    value = m.range.high if d == BULLISH else m.range.low

    if not _finite(value) or value <= 0:
        return None

    correct_side = (
        value > m.price if d == BULLISH else value < m.price
    )

    return value if correct_side else None


def sweep_reversal(
    setup: PAMap,
    trigger: PAMap,
    htf: Optional[dict],
    params: Optional[Any] = None,
) -> List[Playbook]:
    """Liquidity sweep followed by directional confirmation."""
    out = []

    age_setup = _p(
        params, "sweep_max_age_setup", SWEEP_MAX_AGE_SETUP
    )
    age_trigger = _p(
        params, "sweep_max_age_trigger", SWEEP_MAX_AGE_TRIGGER
    )
    conf_min = _p(params, "sweep_conf_min", SWEEP_CONF_MIN)
    weight_quality = _p(params, "w_sweep_quality", 0.36)
    weight_confirmation = _p(params, "w_sweep_conf", 0.30)

    for d in (BULLISH, BEARISH):
        candidates = [
            (sweep, "kurulum")
            for sweep in setup.sweeps
            if sweep.direction == d
            and 0 <= sweep.age <= age_setup
        ]

        if trigger.ok:
            candidates += [
                (sweep, "tetik")
                for sweep in trigger.sweeps
                if sweep.direction == d
                and 0 <= sweep.age <= age_trigger
            ]

        if not candidates:
            continue

        sweep, source = max(
            candidates,
            key=lambda item: (item[0].quality, -item[0].age),
        )

        conf, confirmation_steps = confirmation(
            setup, trigger, d, params
        )

        if conf < conf_min:
            continue

        position = setup.range.position if setup.range else 0.5
        location = clamp(
            1 - position if d == BULLISH else position,
            0.0,
            1.0,
        )

        zones = zones_at_price(
            setup.zones, setup.price, setup.atr, d
        )

        score = (
            weight_quality * sweep.quality
            + weight_confirmation * conf
            + 0.14 * location
            + 0.10 * (1.0 if zones else 0.0)
            + 0.10 * _htf(htf, d)
        )

        target = (
            _opposite_pool(setup, d)
            or _range_target(setup, d)
        )

        steps = [
            f"{source} TF {sweep.kind} likiditesi süpürüldü "
            f"@ {sweep.level:.6g} (kalite {sweep.quality:.2f})",
            *confirmation_steps,
        ]

        if zones:
            steps.append("bölge desteği: " + describe(zones[0]))

        out.append(
            Playbook(
                "sweep_reversal",
                SIDE_OF[d],
                clamp(score, 0, 1),
                steps,
                sweep.extreme,
                target,
            )
        )

    return out


def trend_pullback(
    setup: PAMap,
    trigger: PAMap,
    htf: Optional[dict],
    params: Optional[Any] = None,
) -> List[Playbook]:
    """Established trend retracing into a zone or OTE band."""
    out = []
    structure, rng = setup.structure, setup.range

    if (
        rng is None
        or rng.source != "leg"
        or structure.last_break is None
    ):
        return out

    d = (
        BULLISH if structure.trend == "UP"
        else BEARISH if structure.trend == "DOWN"
        else None
    )

    if (
        d is None
        or structure.last_break.direction != _up(d)
        or structure.last_break.age > 80
    ):
        return out

    htf_strength = _htf(htf, d)

    if htf_strength <= -0.5:
        return out

    position = (
        rng.position if d == BULLISH else 1.0 - rng.position
    )

    if not 0 <= position <= _p(
        params, "pullback_max_pos", PULLBACK_MAX_POS
    ):
        return out

    zones = zones_at_price(
        setup.zones, setup.price, setup.atr, d
    )

    ote = (
        _p(params, "ote_lo", OTE_LO)
        <= position
        <= _p(params, "ote_hi", OTE_HI)
    )

    if not zones and not ote:
        return out

    conf, confirmation_steps = confirmation(
        setup, trigger, d, params
    )

    best = max(zones, key=zone_quality) if zones else None
    impulse = clamp(
        structure.last_break.displacement / 2.0, 0.0, 1.0
    )

    score = (
        _p(params, "w_pullback_impulse", 0.20) * impulse
        + _p(params, "w_pullback_zone", 0.25)
        * (zone_quality(best) if best else 0.5)
        + _p(params, "w_pullback_conf", 0.30) * conf
        + 0.10 * (1.0 if ote else 0.5)
        + 0.15 * max(0.0, htf_strength)
    )

    if conf < _p(
        params, "pullback_conf_min", PULLBACK_CONF_MIN
    ):
        score *= 0.75

    if best is not None:
        invalidation = best.lo if d == BULLISH else best.hi

        if structure.protected is not None:
            invalidation = (
                max(invalidation, structure.protected)
                if d == BULLISH
                else min(invalidation, structure.protected)
            )
    else:
        invalidation = structure.protected

    if invalidation is None:
        return out

    target = rng.high if d == BULLISH else rng.low

    steps = [
        f"trend {structure.trend}: {structure.last_break.kind} "
        f"{structure.last_break.displacement:.1f} ATR "
        "yer değiştirmeyle",
        f"itki bacağının %{(1 - position) * 100:.0f} "
        "geri çekilmesi" + (" (OTE bandı)" if ote else ""),
    ]

    if best is not None:
        steps.append("bölge: " + describe(best))

    steps += confirmation_steps

    out.append(
        Playbook(
            "trend_pullback",
            SIDE_OF[d],
            clamp(score, 0, 1),
            steps,
            invalidation,
            target,
        )
    )

    return out


def breakout_retest(
    setup: PAMap,
    trigger: PAMap,
    htf: Optional[dict],
    params: Optional[Any] = None,
) -> List[Playbook]:
    """Displacement break retested from the opposite side."""
    out = []
    last_break = setup.structure.last_break

    if (
        last_break is None
        or not 2 <= last_break.age <= _p(
            params, "retest_max_age", RETEST_MAX_AGE
        )
        or last_break.displacement < 1.0
    ):
        return out

    d = BULLISH if last_break.direction == "UP" else BEARISH
    atr = setup.atr
    price = setup.price
    level = last_break.level

    if d == BULLISH:
        holding = (
            price > level and price - level <= 0.8 * atr
        )
        retested = (
            setup.recent_low is not None
            and level - 0.8 * atr
            <= setup.recent_low
            <= level + 0.35 * atr
        )
        extreme = min(
            setup.recent_low
            if setup.recent_low is not None
            else level,
            level - 0.3 * atr,
        )
    else:
        holding = (
            price < level and level - price <= 0.8 * atr
        )
        retested = (
            setup.recent_high is not None
            and level - 0.35 * atr
            <= setup.recent_high
            <= level + 0.8 * atr
        )
        extreme = max(
            setup.recent_high
            if setup.recent_high is not None
            else level,
            level + 0.3 * atr,
        )

    if not (holding and retested):
        return out

    conf, confirmation_steps = confirmation(
        setup, trigger, d, params
    )

    if conf < _p(params, "retest_conf_min", RETEST_CONF_MIN):
        return out

    score = (
        0.25 * clamp(last_break.displacement / 2.0, 0, 1)
        + 0.15 * (1.0 if last_break.major else 0.5)
        + 0.35 * conf
        + 0.10 * (
            1.0
            if zones_at_price(setup.zones, price, atr, d)
            else 0.4
        )
        + 0.15 * max(0.0, _htf(htf, d))
    )

    steps = [
        f"{last_break.kind} "
        f"{'yukarı' if d == BULLISH else 'aşağı'} "
        f"@ {level:.6g} "
        f"({last_break.displacement:.1f} ATR, "
        f"{last_break.age} mum önce)"
        + (" — ana seviye" if last_break.major else ""),
        "kırılan seviye ters yönden test edildi ve korunuyor",
        *confirmation_steps,
    ]

    out.append(
        Playbook(
            "breakout_retest",
            SIDE_OF[d],
            clamp(score, 0, 1),
            steps,
            extreme,
            _opposite_pool(setup, d),
        )
    )

    return out


def range_fade(
    setup: PAMap,
    trigger: PAMap,
    regime_name: str,
    params: Optional[Any] = None,
) -> List[Playbook]:
    """Range-edge rejection with a capped target."""
    out = []
    rng = setup.range

    if (
        rng is None
        or rng.source != "window"
        or rng.high - rng.low < MIN_RANGE_ATR * setup.atr
    ):
        return out

    if (
        setup.structure.trend in ("UP", "DOWN")
        and regime_name not in ("RANGE", "TRANSITION")
    ):
        return out

    for d in (BULLISH, BEARISH):
        position = (
            rng.position if d == BULLISH else 1.0 - rng.position
        )
        edge_limit = _p(params, "range_edge", RANGE_EDGE)

        if edge_limit <= 0 or not 0 <= position <= edge_limit:
            continue

        if setup.displacement and setup.displacement[0] != d:
            continue

        conf, confirmation_steps = confirmation(
            setup, trigger, d, params
        )

        swept = [
            sweep
            for sweep in setup.sweeps
            if sweep.direction == d
            and sweep.age <= SWEEP_MAX_AGE_SETUP
        ]

        if swept:
            conf = min(1.0, conf + 0.2)
            confirmation_steps.append(
                f"kenarda likidite süpürmesi "
                f"@ {swept[0].level:.6g}"
            )

        if conf < 0.45:
            continue

        zones = zones_at_price(
            setup.zones, setup.price, setup.atr, d
        )

        edge = clamp(
            1.0 - position / edge_limit, 0.0, 1.0
        )

        score = (
            0.35 * conf
            + 0.25 * edge
            + 0.20 * (1.0 if regime_name == "RANGE" else 0.6)
            + 0.20 * (1.0 if zones or swept else 0.4)
        )

        width = rng.high - rng.low

        if d == BULLISH:
            invalidation = min(
                rng.low, setup.recent_low or rng.low
            )
            target = rng.high - 0.1 * width
            cap = rng.high
        else:
            invalidation = max(
                rng.high, setup.recent_high or rng.high
            )
            target = rng.low + 0.1 * width
            cap = rng.low

        steps = [
            f"yatay aralık {rng.low:.6g}–{rng.high:.6g}, "
            f"fiyat {'alt' if d == BULLISH else 'üst'} kenarda "
            f"(%{rng.position * 100:.0f})",
            *confirmation_steps,
        ]

        out.append(
            Playbook(
                "range_fade",
                SIDE_OF[d],
                clamp(score, 0, 1),
                steps,
                invalidation,
                target,
                cap,
            )
        )

    return out


def _external_sweep_internal_choch(
    setup: PAMap,
    trigger: PAMap,
    direction: str,
) -> bool:
    """External sweep plus trigger-timeframe internal CHoCH."""
    if not trigger.ok:
        return False

    external = any(
        item.direction == direction
        and 0 <= item.age <= 18
        and item.quality >= 0.55
        for item in setup.external_sweeps
    )

    structure = trigger.structure
    internal_age = (
        structure.internal_last_break_age
        if structure.internal_last_break_age is not None
        else 999
    )

    internal_choch = (
        structure.internal_last_break_kind == "CHOCH"
        and structure.internal_last_break_direction
        == _up(direction)
        and internal_age <= 12
    )

    return external and internal_choch


def inducement_sweep(
    setup: PAMap,
    trigger: PAMap,
    htf: Optional[dict],
    params: Optional[Any] = None,
) -> List[Playbook]:
    """Inducement-backed sweep with external/internal confirmation."""
    out = []

    for d in (BULLISH, BEARISH):
        candidates = [
            sweep
            for sweep in setup.sweeps
            if sweep.direction == d
            and sweep.has_inducement
            and sweep.age <= 10
        ]

        if (
            not candidates
            or not _external_sweep_internal_choch(
                setup, trigger, d
            )
        ):
            continue

        sweep = max(
            candidates, key=lambda item: item.quality
        )

        matching_idm = [
            item
            for item in setup.inducements
            if item.direction == d
            and abs(item.index - sweep.index) < 16
        ]

        idm_quality = max(
            (item.quality for item in matching_idm),
            default=0.5,
        )

        conf, confirmation_steps = confirmation(
            setup, trigger, d, params
        )

        if conf < 0.38:
            continue

        score = (
            0.32 * sweep.quality
            + 0.28 * conf
            + 0.22 * idm_quality
            + 0.12 * min(1.0, setup.confluence_score)
            + 0.06
        )

        steps = [
            f"IDM (q={idm_quality:.2f}) + major sweep "
            f"@ {sweep.level:.6g}",
            *confirmation_steps,
            f"inducement quality {idm_quality:.2f}",
        ]

        if matching_idm:
            steps.append(
                f"IDM level {matching_idm[0].level:.6g}"
            )

        out.append(
            Playbook(
                "inducement_sweep",
                SIDE_OF[d],
                clamp(score, 0, 1),
                steps,
                sweep.extreme,
                _opposite_pool(setup, d),
            )
        )

    return out


def ote_pullback(
    setup: PAMap,
    trigger: PAMap,
    htf: Optional[dict],
    params: Optional[Any] = None,
) -> List[Playbook]:
    """Directional impulse-leg OTE retracement."""
    if (
        not setup.range
        or setup.range.source != "leg"
        or not setup.range.ote_zone
    ):
        return []

    out = []

    for d in (BULLISH, BEARISH):
        if setup.structure.trend != _up(d):
            continue

        conf, confirmation_steps = confirmation(
            setup, trigger, d, params
        )

        if conf < 0.32:
            continue

        zones = zones_at_price(
            setup.zones, setup.price, setup.atr, d
        )

        zone_score = max(
            (zone_quality(zone) for zone in zones),
            default=0.35,
        )

        zone_bonus = (
            0.12
            if setup.range.premium_discount == "OTE+ZONE"
            else 0.0
        )

        score = (
            0.30 * conf
            + 0.28 * zone_score
            + 0.22 * (1.0 if setup.range.ote_zone else 0.4)
            + 0.12 * min(1.0, setup.confluence_score)
            + zone_bonus
        )

        invalidation = (
            setup.range.low
            if d == BULLISH
            else setup.range.high
        )

        target = (
            _opposite_pool(setup, d)
            or _range_target(setup, d)
        )

        steps = [
            "Fibonacci OTE (retrace band) "
            f"pos={setup.range.position:.2f}",
            *confirmation_steps,
        ]

        if zones:
            steps.append(
                describe(zones[0])
                + f" q={zones[0].quality:.2f}"
            )

        if setup.range.premium_discount == "OTE+ZONE":
            steps.append("OTE + zone confluence")

        out.append(
            Playbook(
                "ote_pullback",
                SIDE_OF[d],
                clamp(score, 0, 1),
                steps,
                invalidation,
                target,
            )
        )

    return out


def mitigation_retest(
    setup: PAMap,
    trigger: PAMap,
    htf: Optional[dict],
    params: Optional[Any] = None,
) -> List[Playbook]:
    """Retest of a nearby, active, unflipped mitigation block."""
    out = []

    for d in (BULLISH, BEARISH):
        mitigation = [
            zone
            for zone in zones_at_price(
                setup.mitigation_zones,
                setup.price,
                setup.atr,
                d,
            )
            if zone.flipped_at is None
            and zone.quality >= 0.60
        ]

        if not mitigation:
            continue

        zone = max(
            mitigation, key=lambda item: item.quality
        )

        conf, confirmation_steps = confirmation(
            setup, trigger, d, params
        )

        if conf < 0.35:
            continue

        trend_alignment = (
            1.0 if setup.trend_direction == d
            else 0.65 if setup.trend_direction == "NEUTRAL"
            else 0.45
        )

        momentum_alignment = (
            1.0 if setup.momentum == d
            else 0.7 if setup.momentum == "NEUTRAL"
            else 0.5
        )

        score = (
            0.34 * zone.quality
            + 0.26 * conf
            + 0.15 * min(1.0, setup.confluence_score)
            + 0.13 * trend_alignment
            + 0.12 * momentum_alignment
        )

        invalidation = (
            zone.lo if d == BULLISH else zone.hi
        )

        steps = [
            f"mitigation retest q={zone.quality:.2f} "
            f"{describe(zone)}",
            *confirmation_steps,
            f"trend {setup.trend_direction}"
            f"({setup.trend_strength:.2f}) "
            f"mom {setup.momentum}"
            f"({setup.momentum_strength:.2f})",
        ]

        out.append(
            Playbook(
                "mitigation_retest",
                SIDE_OF[d],
                clamp(score, 0, 1),
                steps,
                invalidation,
                _opposite_pool(setup, d),
            )
        )

    return out


def session_cisd(
    setup: PAMap,
    trigger: PAMap,
    htf: Optional[dict],
    params: Optional[Any] = None,
) -> List[Playbook]:
    """Session sweep plus CISD and external/internal confirmation."""
    out = []

    for d in (BULLISH, BEARISH):
        session_range = next(
            (
                item
                for item in setup.sessions
                if item.swept
                and getattr(item, "complete", True)
                and item.sweep_dir == d
                and item.sweep_quality >= 0.65
            ),
            None,
        )

        cisd = next(
            (
                item
                for item in setup.cisds
                if item.direction == d
                and 0 <= item.age <= 8
                and item.quality >= 0.68
                and item.follow_through >= 0.60
            ),
            None,
        )

        if (
            not session_range
            or not cisd
            or not _external_sweep_internal_choch(
                setup, trigger, d
            )
        ):
            continue

        conf, confirmation_steps = confirmation(
            setup, trigger, d, params
        )

        if conf < 0.30:
            continue

        aligned = (
            setup.momentum == d
            or setup.trend_direction == d
        )

        alignment = 1.0 if aligned else 0.55

        score = (
            0.28 * cisd.quality
            + 0.22 * session_range.sweep_quality
            + 0.20 * conf
            + 0.15 * min(1.0, setup.confluence_score)
            + 0.10 * alignment
            + 0.05
        )

        if not aligned:
            score *= 0.78

        invalidation = (
            session_range.low
            if d == BULLISH
            else session_range.high
        )

        steps = [
            f"{session_range.name} sweep "
            f"q={session_range.sweep_quality:.2f} "
            f"+ CISD q={cisd.quality:.2f}",
            *confirmation_steps,
            f"CISD ft={cisd.follow_through:.2f} "
            f"after_sess={cisd.after_session}",
            f"killzone={session_range.killzone or 'outside'} "
            f"overlap={session_range.overlap} "
            f"displacement_quality="
            f"{setup.displacement_quality:.2f}",
        ]

        out.append(
            Playbook(
                "session_cisd",
                SIDE_OF[d],
                clamp(score, 0, 1),
                steps,
                invalidation,
                _opposite_pool(setup, d),
            )
        )

    return out


def find_playbooks(
    setup: PAMap,
    trigger: PAMap,
    htf: Optional[dict],
    regime_name: str,
    params: Optional[Any] = None,
    intelligence: Optional[dict] = None,
) -> List[Playbook]:
    """Generate all eight models, apply confluence, validate and rank."""
    if (
        not setup.ok
        or not _finite(setup.price)
        or setup.price <= 0
    ):
        return []

    if not _finite(setup.atr) or setup.atr <= 0:
        return []

    found = (
        sweep_reversal(setup, trigger, htf, params)
        + trend_pullback(setup, trigger, htf, params)
        + breakout_retest(setup, trigger, htf, params)
        + range_fade(setup, trigger, regime_name, params)
        + inducement_sweep(setup, trigger, htf, params)
        + ote_pullback(setup, trigger, htf, params)
        + mitigation_retest(setup, trigger, htf, params)
        + session_cisd(setup, trigger, htf, params)
    )

    from .confluence import (
        flow_modifier,
        smt_modifier,
        triple_confluence,
    )

    fit = REGIME_FIT.get(regime_name, {})
    intelligence = intelligence or {}
    out = []

    for playbook in found:
        multiplier = fit.get(playbook.name, 1.0)

        if multiplier <= 0:
            continue

        if multiplier < 1.0:
            playbook.steps.append(
                f"rejim uyumu x{multiplier:.2f} "
                f"({regime_name})"
            )

        score = playbook.score * multiplier
        d = BULLISH if playbook.side == "LONG" else BEARISH

        thresholds = intelligence.get("thresholds") or {}
        idm_min = float(
            thresholds.get("idm_min_quality", 0.62)
        )

        if playbook.name == "inducement_sweep":
            matched = [
                item
                for item in setup.inducements
                if item.direction == d
            ]

            if (
                not matched
                or max(item.quality for item in matched) < idm_min
            ):
                continue

        triple = triple_confluence(
            setup,
            d,
            min_idm_quality=idm_min,
            tolerance_atr=float(
                thresholds.get("ote_tolerance_atr", 0.35)
            ),
        )

        if triple["active"]:
            score += triple["score"]
            playbook.steps.extend(triple["reasons"])
            playbook.steps.append(
                "OTE + IDM + Mitigation üçlü confluence aktif"
            )

        smt_bonus, smt_reason = smt_modifier(
            intelligence.get("smt"), d
        )
        flow_bonus, flow_reason = flow_modifier(
            intelligence.get("flow"), d
        )

        score += smt_bonus
        score += flow_bonus

        if intelligence.get("flow"):
            playbook.steps.append(flow_reason)

        if intelligence.get("smt"):
            playbook.steps.append(smt_reason)

        if (
            playbook.name in (
                "session_cisd",
                "mitigation_retest",
            )
            and setup.displacement_quality
            < float(
                thresholds.get("displacement_min", 0.64)
            )
        ):
            score *= 0.85
            playbook.steps.append(
                f"Displacement quality düşük: "
                f"{setup.displacement_quality:.2f} < "
                f"{thresholds.get('displacement_min', 0.64):.2f}"
            )

        model = intelligence.get("market_model") or {}

        if (
            model.get("candidate")
            and model.get("direction") == d
        ):
            score += 0.04
            playbook.steps.append(
                "MMBM/MMSM araştırma modeli ile aynı yön "
                "(yalnızca ek teyit)"
            )

        session_match = next(
            (
                item
                for item in setup.sessions
                if item.swept and item.sweep_dir == d
            ),
            None,
        )

        if session_match and session_match.overlap:
            score *= 1.08
            playbook.steps.append(
                "London / New York overlap sırasında session sweep"
            )

        elif session_match and session_match.killzone:
            score *= 1.04
            playbook.steps.append(
                f"Killzone teyidi: {session_match.killzone}"
            )

        if (
            session_match
            and (
                intelligence.get("smt") or {}
            ).get("direction") == d
        ):
            score += 0.03
            playbook.steps.append(
                "SMT + Session sweep birleşik teyidi"
            )

        if (
            setup.flow_divergence.get("direction") == d
            and any(
                item.direction == d and item.age <= 12
                for item in setup.sweeps
            )
        ):
            score += 0.04
            playbook.steps.append(
                "Fiyat / candle-delta divergence "
                "+ likidite sweep teyidi"
            )

        if not _finite(score):
            continue

        playbook.score = round(
            clamp(score, 0.0, 1.0), 3
        )
        out.append(playbook)

    # Validate candidate geometry here.
    # Final stop buffers, costs and style-specific R/R are
    # evaluated by build_plan(), not by this candidate layer.
    return filter_valid_playbooks(
        out,
        setup.price,
        min_rr=0.0,
        require_target=False,
    )


def validate_playbook(
    playbook: Playbook,
    entry_price: float,
    min_rr: float = 1.2,
    *,
    require_target: bool = True,
) -> Tuple[bool, str]:
    """Validate candidate geometry without placing an order.

    Default: explicit target and at least 1.2 raw R.

    find_playbooks uses min_rr=0 and allows a missing candidate
    target because build_plan applies the final target rules.
    """
    if not isinstance(playbook, Playbook):
        return False, "INVALID_PLAYBOOK_TYPE"

    if playbook.side not in ("LONG", "SHORT"):
        return False, "INVALID_SIDE"

    if not all(
        _finite(value)
        for value in (
            entry_price,
            playbook.invalidation,
            playbook.score,
            min_rr,
        )
    ):
        return False, "NON_FINITE_INPUT"

    if entry_price <= 0 or playbook.invalidation <= 0:
        return False, "INVALID_PRICE"

    if not 0 <= playbook.score <= 1:
        return False, "INVALID_SCORE"

    if min_rr < 0:
        return False, "INVALID_MIN_RR"

    long_ = playbook.side == "LONG"

    if long_ and playbook.invalidation >= entry_price:
        return False, "INVALID_LONG_STOP"

    if not long_ and playbook.invalidation <= entry_price:
        return False, "INVALID_SHORT_STOP"

    if playbook.cap is not None:
        if not _finite(playbook.cap) or playbook.cap <= 0:
            return False, "INVALID_CAP"

        wrong_cap = (
            playbook.cap <= entry_price
            if long_
            else playbook.cap >= entry_price
        )

        if wrong_cap:
            return False, "INVALID_CAP_DIRECTION"

    if playbook.target is None:
        if require_target:
            return False, "MISSING_TARGET"
        return True, "VALID_PENDING_PLAN"

    if not _finite(playbook.target) or playbook.target <= 0:
        return False, "INVALID_TARGET"

    if long_ and playbook.target <= entry_price:
        return False, "INVALID_LONG_TARGET"

    if not long_ and playbook.target >= entry_price:
        return False, "INVALID_SHORT_TARGET"

    if playbook.cap is not None:
        exceeds_cap = (
            playbook.target > playbook.cap
            if long_
            else playbook.target < playbook.cap
        )

        if exceeds_cap:
            return False, "TARGET_EXCEEDS_CAP"

    risk = abs(entry_price - playbook.invalidation)
    reward = abs(playbook.target - entry_price)
    rr = reward / risk

    if not math.isfinite(rr) or rr < min_rr:
        return False, "INSUFFICIENT_RISK_REWARD"

    return True, "VALID"


def filter_valid_playbooks(
    playbooks: List[Playbook],
    entry_price: float,
    min_rr: float = 1.2,
    *,
    require_target: bool = True,
) -> List[Playbook]:
    """Return valid candidates in stable descending score order."""
    return sorted(
        (
            playbook
            for playbook in playbooks
            if validate_playbook(
                playbook,
                entry_price,
                min_rr,
                require_target=require_target,
            )[0]
        ),
        key=lambda playbook: -playbook.score,
    )


# PLAYBOOKS_PY_END
