"""Playbooks: complete price-action setup models.

A playbook is not a vote, it is a story that has to be complete before a trade exists:

  sweep_reversal   liquidity is taken (stops run beyond a pool), price is rejected back inside and the lower timeframe
                   confirms with a structure shift / reversal candle. Target: the liquidity on the other side.
  trend_pullback   an established trend (BOS with displacement) retraces into discount/premium of its impulse leg,
                   into a fresh order block / FVG or the OTE band, and the trigger timeframe turns back with the trend.
  breakout_retest  a displacement break of structure is retested from the other side and holds.
  range_fade       in a range, price reaches the edge, is rejected, and targets the middle / opposite edge.

Every playbook carries its invalidation (where the idea is wrong) and its natural target; the planner turns that into
an executable plan. Scores are 0..1 and say how complete and clean the model is, not a probability.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple, Any

from ...models import BEARISH, BULLISH, SIDE_OF
from ...util import clamp
from .types import PAMap, Playbook
from .zones import describe, zone_quality, zones_at_price

# Default constants (overridable via ParameterSet injection)
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
    "TREND_UP": {"trend_pullback": 1.0, "breakout_retest": 1.0, "sweep_reversal": 0.9, "range_fade": 0.0,
                 "inducement_sweep": 1.05, "ote_pullback": 1.1, "mitigation_retest": 1.0},
    "TREND_DOWN": {"trend_pullback": 1.0, "breakout_retest": 1.0, "sweep_reversal": 0.9, "range_fade": 0.0,
                   "inducement_sweep": 1.05, "ote_pullback": 1.1, "mitigation_retest": 1.0},
    "RANGE": {"trend_pullback": 0.75, "breakout_retest": 0.8, "sweep_reversal": 1.0, "range_fade": 1.0,
              "inducement_sweep": 1.0, "ote_pullback": 0.8, "mitigation_retest": 0.9},
    "TRANSITION": {"trend_pullback": 0.9, "breakout_retest": 0.9, "sweep_reversal": 1.0, "range_fade": 0.8,
                   "inducement_sweep": 1.05, "ote_pullback": 0.95, "mitigation_retest": 1.0},
    "VOLATILE": {"trend_pullback": 0.85, "breakout_retest": 0.8, "sweep_reversal": 0.85, "range_fade": 0.6,
                 "inducement_sweep": 0.9, "ote_pullback": 0.85, "mitigation_retest": 0.9},
}


def _p(params: Optional[Any], name: str, default):
    """Read a value from an injected ParameterSet or fall back to the module default."""
    if params is None:
        return default
    return getattr(params, name, default)


def _up(d: str) -> str:
    return "UP" if d == BULLISH else "DOWN"


def confirmation(setup: PAMap, trigger: PAMap, d: str, params: Optional[Any] = None) -> Tuple[float, List[str]]:
    """How clearly the lower timeframe (and the latest setup candle) turned in direction `d`. 0..1."""
    max_age = _p(params, "trigger_break_max_age", TRIGGER_BREAK_MAX_AGE)
    score, steps = 0.0, []
    if trigger.ok:
        lb = trigger.structure.last_break
        if lb and lb.age <= max_age and lb.direction == _up(d):
            score = max(score, 0.75 if lb.kind == "CHOCH" else 0.6)
            steps.append(f"tetik TF {lb.kind} {'yukarı' if d == BULLISH else 'aşağı'} ({lb.age} mum önce)")
        if trigger.candle.direction == d:
            score = min(1.0, max(score, trigger.candle.strength) + (0.15 if score else 0.0))
            steps.append("tetik mumu: " + ", ".join(trigger.candle.patterns))
        if trigger.displacement and trigger.displacement[0] == d:
            score = min(1.0, max(score, 0.55) + (0.05 if score else 0.0))
            steps.append(f"tetik TF yer değiştirme {trigger.displacement[1]:.1f} ATR")
    if setup.candle.direction == d:
        score = min(1.0, max(score, setup.candle.strength * 0.9) + (0.1 if score else 0.0))
        steps.append("kurulum mumu: " + ", ".join(setup.candle.patterns))
    return score, steps


def _htf(htf: Optional[dict], d: str) -> float:
    """+strength when the higher timeframes agree with `d`, -strength when they oppose it."""
    if not htf or htf.get("direction") not in (BULLISH, BEARISH):
        return 0.0
    s = float(htf.get("strength") or 0.0)
    return s if htf["direction"] == d else -s


def _opposite_pool(m: PAMap, d: str) -> Optional[float]:
    pools = m.pools_above if d == BULLISH else m.pools_below
    return pools[0].price if pools else None


def sweep_reversal(setup: PAMap, trigger: PAMap, htf: Optional[dict], params: Optional[Any] = None) -> List[Playbook]:
    out = []
    age_setup = _p(params, "sweep_max_age_setup", SWEEP_MAX_AGE_SETUP)
    age_trig = _p(params, "sweep_max_age_trigger", SWEEP_MAX_AGE_TRIGGER)
    conf_min = _p(params, "sweep_conf_min", SWEEP_CONF_MIN)
    w_q = _p(params, "w_sweep_quality", 0.36)
    w_c = _p(params, "w_sweep_conf", 0.30)
    for d in (BULLISH, BEARISH):
        cands = [(s, "kurulum") for s in setup.sweeps if s.direction == d and s.age <= age_setup]
        if trigger.ok:
            cands += [(s, "tetik") for s in trigger.sweeps if s.direction == d and s.age <= age_trig]
        if not cands:
            continue
        sw, where = max(cands, key=lambda x: (x[0].quality, -x[0].age))
        conf, csteps = confirmation(setup, trigger, d, params)
        if conf < conf_min:
            continue
        pos = setup.range.position if setup.range else 0.5
        location = clamp((1 - pos) if d == BULLISH else pos, 0.0, 1.0)
        zones = zones_at_price(setup.zones, setup.price, setup.atr, d)
        h = _htf(htf, d)
        score = w_q * sw.quality + w_c * conf + 0.14 * location + 0.10 * (1.0 if zones else 0.0) + 0.10 * h
        target = _opposite_pool(setup, d) or (setup.range.high if d == BULLISH else setup.range.low if setup.range else None)
        steps = [f"{where} TF {sw.kind} likiditesi süpürüldü @ {sw.level:.6g} (kalite {sw.quality:.2f})", *csteps]
        if zones:
            steps.append("bölge desteği: " + describe(zones[0]))
        out.append(Playbook("sweep_reversal", SIDE_OF[d], clamp(score, 0, 1), steps, sw.extreme, target))
    return out


def trend_pullback(setup: PAMap, trigger: PAMap, htf: Optional[dict], params: Optional[Any] = None) -> List[Playbook]:
    out = []
    st, rng = setup.structure, setup.range
    if rng is None or rng.source != "leg" or st.last_break is None:
        return out
    d = BULLISH if st.trend == "UP" else BEARISH if st.trend == "DOWN" else None
    if d is None or st.last_break.direction != _up(d) or st.last_break.age > 80:
        return out
    h = _htf(htf, d)
    if h <= -0.5:
        return out                                     # pulling back against a clearly opposite higher timeframe
    pos = rng.position if d == BULLISH else 1.0 - rng.position
    if not 0.0 <= pos <= PULLBACK_MAX_POS:
        return out
    zones = zones_at_price(setup.zones, setup.price, setup.atr, d)
    ote = OTE_LO <= pos <= OTE_HI
    if not zones and not ote:
        return out
    conf, csteps = confirmation(setup, trigger, d)
    best = max(zones, key=zone_quality) if zones else None
    impulse = clamp(st.last_break.displacement / 2.0, 0.0, 1.0)
    score = (0.20 * impulse + 0.25 * (zone_quality(best) if best else 0.5) + 0.30 * conf
             + 0.10 * (1.0 if ote else 0.5) + 0.15 * max(0.0, h))
    if conf < 0.3:
        score *= 0.75                                  # no turn on the trigger timeframe yet
    if best is not None:
        invalidation = best.lo if d == BULLISH else best.hi
        if st.protected is not None:                   # never beyond the level that ends the trend
            invalidation = max(invalidation, st.protected) if d == BULLISH else min(invalidation, st.protected)
    else:
        invalidation = st.protected
    if invalidation is None:
        return out
    target = rng.high if d == BULLISH else rng.low
    steps = [f"trend {st.trend}: {st.last_break.kind} {st.last_break.displacement:.1f} ATR yer değiştirmeyle",
             f"itki bacağının %{(1 - pos) * 100:.0f} geri çekilmesi" + (" (OTE bandı)" if ote else "")]
    if best is not None:
        steps.append("bölge: " + describe(best))
    steps += csteps
    out.append(Playbook("trend_pullback", SIDE_OF[d], clamp(score, 0, 1), steps, invalidation, target))
    return out


def breakout_retest(setup: PAMap, trigger: PAMap, htf: Optional[dict], params: Optional[Any] = None) -> List[Playbook]:
    out = []
    lb = setup.structure.last_break
    if lb is None or not 2 <= lb.age <= RETEST_MAX_AGE or lb.displacement < 1.0:
        return out
    d = BULLISH if lb.direction == "UP" else BEARISH
    a, price, level = setup.atr, setup.price, lb.level
    if d == BULLISH:
        holding = price > level and price - level <= 0.8 * a
        retested = setup.recent_low is not None and level - 0.8 * a <= setup.recent_low <= level + 0.35 * a
        extreme = min(setup.recent_low if setup.recent_low is not None else level, level - 0.3 * a)
    else:
        holding = price < level and level - price <= 0.8 * a
        retested = setup.recent_high is not None and level - 0.35 * a <= setup.recent_high <= level + 0.8 * a
        extreme = max(setup.recent_high if setup.recent_high is not None else level, level + 0.3 * a)
    if not (holding and retested):
        return out
    conf, csteps = confirmation(setup, trigger, d)
    if conf < 0.4:
        return out
    h = _htf(htf, d)
    score = (0.25 * clamp(lb.displacement / 2.0, 0, 1) + 0.15 * (1.0 if lb.major else 0.5) + 0.35 * conf
             + 0.10 * (1.0 if zones_at_price(setup.zones, price, a, d) else 0.4) + 0.15 * max(0.0, h))
    steps = [f"{lb.kind} {'yukarı' if d == BULLISH else 'aşağı'} @ {level:.6g} ({lb.displacement:.1f} ATR, {lb.age} mum önce)"
             + (" — ana seviye" if lb.major else ""), "kırılan seviye ters yönden test edildi ve korunuyor", *csteps]
    out.append(Playbook("breakout_retest", SIDE_OF[d], clamp(score, 0, 1), steps, extreme, _opposite_pool(setup, d)))
    return out


def range_fade(setup: PAMap, trigger: PAMap, regime_name: str, params: Optional[Any] = None) -> List[Playbook]:
    out = []
    rng = setup.range
    if rng is None or rng.source != "window" or rng.high - rng.low < MIN_RANGE_ATR * setup.atr:
        return out
    if setup.structure.trend in ("UP", "DOWN") and regime_name not in ("RANGE", "TRANSITION"):
        return out
    for d in (BULLISH, BEARISH):
        pos = rng.position if d == BULLISH else 1.0 - rng.position
        if pos > RANGE_EDGE:
            continue
        if setup.displacement and setup.displacement[0] != d:
            continue                                   # a displacement candle through the edge is a breakout, not a fade
        conf, csteps = confirmation(setup, trigger, d)
        swept = [s for s in setup.sweeps if s.direction == d and s.age <= SWEEP_MAX_AGE_SETUP]
        if swept:
            conf = min(1.0, conf + 0.2)
            csteps.append(f"kenarda likidite süpürmesi @ {swept[0].level:.6g}")
        if conf < 0.45:
            continue
        zones = zones_at_price(setup.zones, setup.price, setup.atr, d)
        edge = clamp(1.0 - pos / RANGE_EDGE, 0.0, 1.0)
        score = 0.35 * conf + 0.25 * edge + 0.20 * (1.0 if regime_name == "RANGE" else 0.6) + 0.20 * (1.0 if zones or swept else 0.4)
        width = rng.high - rng.low
        if d == BULLISH:
            invalidation, target, cap = min(rng.low, setup.recent_low or rng.low), rng.high - 0.1 * width, rng.high
        else:
            invalidation, target, cap = max(rng.high, setup.recent_high or rng.high), rng.low + 0.1 * width, rng.low
        steps = [f"yatay aralık {rng.low:.6g}–{rng.high:.6g}, fiyat {'alt' if d == BULLISH else 'üst'} kenarda (%{rng.position * 100:.0f})", *csteps]
        out.append(Playbook("range_fade", SIDE_OF[d], clamp(score, 0, 1), steps, invalidation, target, cap))
    return out


def _external_sweep_internal_choch(setup: PAMap, trigger: PAMap, direction: str) -> bool:
    """Advanced setups require a major/external sweep plus a trigger-TF internal CHoCH."""
    external = any(x.direction == direction and x.age <= 18 and x.quality >= 0.55 for x in setup.external_sweeps)
    br = trigger.structure
    internal_choch = (br.internal_last_break_kind == "CHOCH" and
                       br.internal_last_break_direction == _up(direction) and
                       (br.internal_last_break_age if br.internal_last_break_age is not None else 999) <= 12)
    return external and internal_choch


def inducement_sweep(setup: PAMap, trigger: PAMap, htf: Optional[dict], params: Optional[Any] = None) -> List[Playbook]:
    """High-quality sweep that took a confirmed internal inducement before the major pool."""
    out = []
    for d in (BULLISH, BEARISH):
        # Prefer sweeps that have high-quality IDM
        cands = [s for s in setup.sweeps if s.direction == d and s.has_inducement and s.age <= 10]
        if not cands or not _external_sweep_internal_choch(setup, trigger, d):
            continue
        s = max(cands, key=lambda x: x.quality)
        # Find the best matching IDM
        matching_idm = [idm for idm in setup.inducements if idm.direction == d and abs(idm.index - s.index) < 16]
        idm_q = max((idm.quality for idm in matching_idm), default=0.5)
        conf, csteps = confirmation(setup, trigger, d, params)
        if conf < 0.38:
            continue
        target = _opposite_pool(setup, d)
        # Stronger score when IDM quality is high and confluence exists
        score = (0.32 * s.quality + 0.28 * conf + 0.22 * idm_q +
                 0.12 * min(1.0, setup.confluence_score) + 0.06)
        inv = s.extreme
        steps = [
            f"IDM (q={idm_q:.2f}) + major sweep @ {s.level:.6g}",
            *csteps,
            f"inducement quality {idm_q:.2f}",
        ]
        if matching_idm:
            steps.append(f"IDM level {matching_idm[0].level:.6g}")
        out.append(Playbook("inducement_sweep", SIDE_OF[d], clamp(score, 0, 1), steps, inv, target))
    return out


def ote_pullback(setup: PAMap, trigger: PAMap, htf: Optional[dict], params: Optional[Any] = None) -> List[Playbook]:
    """Trend pullback into Fibonacci OTE (0.618-0.786 retrace of the impulse leg), preferably with zone confluence."""
    if not setup.range or not setup.range.ote_zone:
        return []
    out = []
    for d in (BULLISH, BEARISH):
        if setup.structure.trend != _up(d):
            continue
        conf, csteps = confirmation(setup, trigger, d, params)
        if conf < 0.32:
            continue
        zones = zones_at_price(setup.zones, setup.price, setup.atr, d)
        zq = max((zone_quality(z) for z in zones), default=0.35)
        # Bonus for OTE+ZONE
        zone_bonus = 0.12 if setup.range.premium_discount == "OTE+ZONE" else 0.0
        score = (0.30 * conf + 0.28 * zq + 0.22 * (1.0 if setup.range.ote_zone else 0.4) +
                 0.12 * min(1.0, setup.confluence_score) + zone_bonus)
        inv = setup.range.low if d == BULLISH else setup.range.high
        target = _opposite_pool(setup, d) or (setup.range.high if d == BULLISH else setup.range.low)
        steps = [
            f"Fibonacci OTE (retrace band) pos={setup.range.position:.2f}",
            *csteps,
        ]
        if zones:
            steps.append(describe(zones[0]) + f" q={zones[0].quality:.2f}")
        if setup.range.premium_discount == "OTE+ZONE":
            steps.append("OTE + zone confluence")
        out.append(Playbook("ote_pullback", SIDE_OF[d], clamp(score, 0, 1), steps, inv, target))
    return out


def mitigation_retest(setup: PAMap, trigger: PAMap, htf: Optional[dict], params: Optional[Any] = None) -> List[Playbook]:
    """Deep retest of a high-quality mitigation block, preferably aligned with trend and momentum."""
    out = []
    for d in (BULLISH, BEARISH):
        mits = [z for z in setup.mitigation_zones if z.direction == d and z.quality >= 0.60]
        if not mits:
            continue
        z = max(mits, key=lambda x: x.quality)
        conf, csteps = confirmation(setup, trigger, d, params)
        if conf < 0.35:
            continue
        # Alignment bonuses
        trend_align = 1.0 if setup.trend_direction == d else 0.65 if setup.trend_direction == "NEUTRAL" else 0.45
        mom_align = 1.0 if setup.momentum == d else 0.7 if setup.momentum == "NEUTRAL" else 0.5
        score = (0.34 * z.quality + 0.26 * conf + 0.15 * min(1.0, setup.confluence_score) +
                 0.13 * trend_align + 0.12 * mom_align)
        inv = z.lo if d == BULLISH else z.hi
        target = _opposite_pool(setup, d)
        steps = [
            f"mitigation retest q={z.quality:.2f} {describe(z)}",
            *csteps,
            f"trend {setup.trend_direction}({setup.trend_strength:.2f}) mom {setup.momentum}({setup.momentum_strength:.2f})",
        ]
        out.append(Playbook("mitigation_retest", SIDE_OF[d], clamp(score, 0, 1), steps, inv, target))
    return out


def session_cisd(setup: PAMap, trigger: PAMap, htf: Optional[dict], params: Optional[Any] = None) -> List[Playbook]:
    """High-quality session liquidity sweep followed by deep CISD confirmation."""
    out = []
    for d in (BULLISH, BEARISH):
        sess = next((s for s in setup.sessions if s.swept and s.sweep_dir == d and s.sweep_quality >= 0.65), None)
        cisd = next((c for c in setup.cisds if c.direction == d and c.age <= 8 and c.quality >= 0.68 and c.follow_through >= 0.60), None)
        if not sess or not cisd or not _external_sweep_internal_choch(setup, trigger, d):
            continue
        conf, csteps = confirmation(setup, trigger, d, params)
        if conf < 0.30:
            continue
        aligned = setup.momentum == d or setup.trend_direction == d
        align = 1.0 if aligned else 0.55
        score = (0.28 * cisd.quality + 0.22 * sess.sweep_quality + 0.20 * conf +
                 0.15 * min(1.0, setup.confluence_score) + 0.10 * align + 0.05)
        if not aligned:
            score *= 0.78
        inv = sess.low if d == BULLISH else sess.high
        target = _opposite_pool(setup, d)
        steps = [
            f"{sess.name} sweep q={sess.sweep_quality:.2f} + CISD q={cisd.quality:.2f}",
            *csteps,
            f"CISD ft={cisd.follow_through:.2f} after_sess={cisd.after_session}",
            f"killzone={sess.killzone or 'outside'} overlap={sess.overlap} displacement_quality={setup.displacement_quality:.2f}",
        ]
        out.append(Playbook("session_cisd", SIDE_OF[d], clamp(score, 0, 1), steps, inv, target))
    return out


def find_playbooks(setup: PAMap, trigger: PAMap, htf: Optional[dict], regime_name: str,
                   params: Optional[Any] = None, intelligence: Optional[dict] = None) -> List[Playbook]:
    """All complete playbooks, regime-fitted, best first.
    `params` is an optional ParameterSet that overrides age windows, confirmation minima and score weights.
    """
    if not setup.ok:
        return []
    found = (sweep_reversal(setup, trigger, htf, params)
             + trend_pullback(setup, trigger, htf, params)
             + breakout_retest(setup, trigger, htf, params)
             + range_fade(setup, trigger, regime_name, params)
             + inducement_sweep(setup, trigger, htf, params)
             + ote_pullback(setup, trigger, htf, params)
             + mitigation_retest(setup, trigger, htf, params)
             + session_cisd(setup, trigger, htf, params))
    fit = REGIME_FIT.get(regime_name, {})
    out = []
    intelligence = intelligence or {}
    from .confluence import flow_modifier, smt_modifier, triple_confluence
    for pb in found:
        mult = fit.get(pb.name, 1.0)
        if mult <= 0:
            continue
        if mult < 1.0:
            pb.steps.append(f"rejim uyumu x{mult:.2f} ({regime_name})")
        score = pb.score * mult
        d = BULLISH if pb.side == "LONG" else BEARISH
        thresholds = intelligence.get("thresholds") or {}
        idm_min = float(thresholds.get("idm_min_quality", 0.62))
        if pb.name == "inducement_sweep":
            matched = [x for x in setup.inducements if x.direction == d]
            if not matched or max(x.quality for x in matched) < idm_min:
                continue
        triple = triple_confluence(setup, d, min_idm_quality=idm_min,
                                   tolerance_atr=float(thresholds.get("ote_tolerance_atr", 0.35)))
        if triple["active"]:
            score += triple["score"]
            pb.steps.extend(triple["reasons"])
            pb.steps.append("OTE + IDM + Mitigation üçlü confluence aktif")
        smt_bonus, smt_reason = smt_modifier(intelligence.get("smt"), d)
        score += smt_bonus
        flow_bonus, flow_reason = flow_modifier(intelligence.get("flow"), d)
        score += flow_bonus
        if intelligence.get("flow"):
            pb.steps.append(flow_reason)
        if intelligence.get("smt"):
            pb.steps.append(smt_reason)
        if pb.name in ("session_cisd", "mitigation_retest") and setup.displacement_quality < float(thresholds.get("displacement_min", 0.64)):
            score *= 0.85
            pb.steps.append(f"Displacement quality düşük: {setup.displacement_quality:.2f} < {thresholds.get('displacement_min', 0.64):.2f}")
        model = intelligence.get("market_model") or {}
        if model.get("candidate") and model.get("direction") == d:
            score += 0.04
            pb.steps.append("MMBM/MMSM araştırma modeli ile aynı yön (yalnızca ek teyit)")
        # Killzone bonus applies only to setups backed by a session sweep and is capped.
        session_match = next((x for x in setup.sessions if x.swept and x.sweep_dir == d), None)
        if session_match and session_match.overlap:
            score *= 1.08
            pb.steps.append("London / New York overlap sırasında session sweep")
        elif session_match and session_match.killzone:
            score *= 1.04
            pb.steps.append(f"Killzone teyidi: {session_match.killzone}")
        if session_match and (intelligence.get("smt") or {}).get("direction") == d:
            score += 0.03
            pb.steps.append("SMT + Session sweep birleşik teyidi")
        if setup.flow_divergence.get("direction") == d and any(x.direction == d and x.age <= 12 for x in setup.sweeps):
            score += 0.04
            pb.steps.append("Fiyat / candle-delta divergence + likidite sweep teyidi")
        pb.score = round(clamp(score, 0.0, 1.0), 3)
        out.append(pb)
    return sorted(out, key=lambda p: -p.score)

