"""build_map(): one pass over a candle series that produces the complete price-action picture (PAMap)."""
from __future__ import annotations

from typing import Sequence

from ...models import BEARISH, BULLISH, Candle
from ..indicators import atr_series
from .candles import candle_signal
from .confluence import candle_flow_divergence, displacement_quality
from .cisd import find_cisds
from .inducement import find_inducements
from .liquidity import _clusters, _tolerance, find_pools, find_sweeps
from .sessions import find_sessions
from .structure import MINOR_LEFT, MINOR_RIGHT, analyze_structure, dealing_range, find_swings
from .types import Level, PAMap
from .zones import find_mitigation_zones, find_zones, zone_quality



MIN_CANDLES = 60
WINDOW = 300


def build_map(candles: Sequence[Candle]) -> PAMap:
    cs = list(candles[-WINDOW:])
    if len(cs) < MIN_CANDLES:
        return PAMap(ok=False, reason=f"need {MIN_CANDLES} candles, have {len(cs)}", n=len(cs))
    atrs = atr_series(cs)
    if len(atrs) != len(cs) or atrs[-1] <= 0:
        return PAMap(ok=False, reason="ATR unavailable", n=len(cs))
    a, price = atrs[-1], cs[-1].c
    swings = find_swings(cs)
    minor = find_swings(cs, MINOR_LEFT, MINOR_RIGHT)
    st = analyze_structure(cs, swings, atrs)
    # Nested internal structure uses independently confirmed minor swings.
    internal = analyze_structure(cs, minor, atrs, right=MINOR_RIGHT)
    st.external_trend = st.trend
    st.internal_trend = internal.trend
    if internal.last_break is not None:
        st.internal_last_break_kind = internal.last_break.kind
        st.internal_last_break_direction = internal.last_break.direction
        st.internal_last_break_age = internal.last_break.age
    above, below = find_pools(cs, minor, a)
    sweeps = find_sweeps(cs, minor, a)
    external_sweeps = find_sweeps(cs, swings, a)
    # Dealing range first so IDM can use it for internal filter
    dr = dealing_range(cs, st, a)
    inducements = find_inducements(
        cs, minor, sweeps, above, below, a,
        dealing_low=dr.low if dr else None,
        dealing_high=dr.high if dr else None,
    )
    # Mark sweeps that had high-quality IDM
    for s in sweeps:
        s.has_inducement = any(
            abs(idm.index - s.index) < 14 and idm.direction == s.direction and idm.quality >= 0.60
            for idm in inducements
        )

    last = cs[-1]
    disp = None
    if last.body >= 1.2 * a and last.body >= 0.6 * max(last.range, 1e-12):
        disp = (BULLISH if last.bullish else BEARISH, round(last.body / a, 2))
    tol = _tolerance(price, a)
    levels = [Level(p, t) for p, t, _ in _clusters([s for s in swings if s.i >= len(cs) - 200], tol, True) + _clusters(
        [s for s in swings if s.i >= len(cs) - 200], tol, False) if t >= 2]

    zones = find_zones(cs, atrs)
    for z in zones:
        z.quality = zone_quality(z)
        z.mean_threshold = (z.lo + z.hi) / 2.0
    mitigation = find_mitigation_zones(cs, atrs, zones)
    for m in mitigation:
        m.quality = zone_quality(m)
        m.mean_threshold = (m.lo + m.hi) / 2.0

    disp_facts = displacement_quality(cs, a)
    flow_div = candle_flow_divergence(cs)
    # Session liquidity + deep CISD
    sessions = find_sessions(cs, a)
    cisds = find_cisds(cs, atrs, sweeps, a, structure_protected=st.protected)
    # Mark CISD that occurred after high-quality session sweep
    for cisd in cisds:
        for sess in sessions:
            if sess.swept and sess.sweep_dir == cisd.direction and abs(cisd.index - sess.end_i) < 18 and sess.sweep_quality >= 0.55:
                cisd.after_session = True
                cisd.quality = min(0.96, cisd.quality + 0.07)


    # Enhance OTE with zone confluence
    if dr and dr.ote_zone:
        nearby_zones = [z for z in zones + mitigation if abs((z.lo + z.hi) / 2 - price) < 1.2 * a and z.quality >= 0.6]
        if nearby_zones:
            dr.premium_discount = "OTE+ZONE"



    # Momentum & Trend direction (PA-level, independent of agents)
    # Trend from structure
    trend_dir = "NEUTRAL"
    trend_str = 0.0
    if st.trend == "UP":
        trend_dir = "BULLISH"
        lb = st.last_break
        if lb and lb.kind == "BOS" and lb.age <= 30:
            trend_str = min(1.0, 0.55 + 0.25 * min(1.0, lb.displacement / 1.8) + 0.2 * (1.0 - min(1.0, lb.age / 40)))
        else:
            trend_str = 0.45
    elif st.trend == "DOWN":
        trend_dir = "BEARISH"
        lb = st.last_break
        if lb and lb.kind == "BOS" and lb.age <= 30:
            trend_str = min(1.0, 0.55 + 0.25 * min(1.0, lb.displacement / 1.8) + 0.2 * (1.0 - min(1.0, lb.age / 40)))
        else:
            trend_str = 0.45

    # Momentum from recent displacement + candle
    mom_dir = "NEUTRAL"
    mom_str = 0.0
    csig = candle_signal(cs, a)
    if disp:
        mom_dir = disp[0]
        mom_str = min(1.0, disp[1] / 2.2)
    if csig.direction != "NEUTRAL":
        if mom_dir == "NEUTRAL":
            mom_dir = csig.direction
            mom_str = csig.strength * 0.7
        elif mom_dir == csig.direction:
            mom_str = min(1.0, mom_str + 0.15 * csig.strength)


    # Simple confluence score (0-1) now including momentum + trend alignment
    conf = 0.0
    if st.trend in ("UP", "DOWN"):
        conf += 0.18
    if any(s.has_inducement for s in sweeps[:2]):
        conf += 0.16
    if dr and dr.ote_zone:
        conf += 0.16
    if any(z.quality >= 0.65 for z in zones[:4] + mitigation[:2]):
        conf += 0.15
    if disp and disp[1] >= 1.5:
        conf += 0.08
    # Momentum + trend alignment bonus
    if mom_dir == trend_dir and mom_dir != "NEUTRAL":
        conf += 0.12 * (0.6 + 0.4 * mom_str)
    elif mom_dir != "NEUTRAL" and trend_dir != "NEUTRAL" and mom_dir != trend_dir:
        conf -= 0.06
    # Session + CISD bonuses (quality-weighted)
    if any(s.swept and s.sweep_quality >= 0.6 for s in sessions):
        conf += 0.10
    if any(c.quality >= 0.72 and (c.after_sweep or c.after_session) for c in cisds):
        conf += 0.12
    conf = max(0.0, min(1.0, conf))




    return PAMap(
        ok=True, n=len(cs), price=price, atr=a, swings=swings, minor=minor, structure=st,
        range=dr, pools_above=above, pools_below=below, sweeps=sweeps, external_sweeps=external_sweeps,
        zones=zones + mitigation, candle=csig, displacement=disp,

        levels=sorted(levels, key=lambda lv: lv.price),
        recent_low=min(c.l for c in cs[-8:]), recent_high=max(c.h for c in cs[-8:]),
        inducements=inducements, mitigation_zones=mitigation, confluence_score=round(conf, 3),
        momentum=mom_dir, momentum_strength=round(mom_str, 3),
        trend_direction=trend_dir, trend_strength=round(trend_str, 3),
        sessions=sessions, cisds=cisds, displacement_quality=disp_facts["score"], displacement_facts=disp_facts, flow_divergence=flow_div,
    )




def summary(m: PAMap) -> dict:
    """Compact, JSON-safe description (decision log, Telegram /analiz, dashboard)."""
    if not m.ok:
        return {"ok": False, "reason": m.reason}
    st = m.structure
    lb = st.last_break
    return {
        "ok": True, "price": m.price, "atr": round(m.atr, 8), "trend": st.trend, "swing_bias": st.swing_bias,
        "internal_trend": st.internal_trend, "external_trend": st.external_trend,
        "internal_last_break": {"kind": st.internal_last_break_kind, "direction": st.internal_last_break_direction, "age": st.internal_last_break_age},
        "external_sweeps": [{"direction": x.direction, "level": x.level, "quality": x.quality, "age": x.age} for x in m.external_sweeps[:3]],
        "displacement_quality": m.displacement_quality, "displacement_facts": m.displacement_facts, "flow_divergence": m.flow_divergence,
        "last_break": {"kind": lb.kind, "dir": lb.direction, "level": lb.level, "age": lb.age, "disp": round(lb.displacement, 2)} if lb else None,
        "protected": st.protected,
        "range": {"high": m.range.high, "low": m.range.low, "position": round(m.range.position, 3),
                  "source": m.range.source, "ote": m.range.ote_zone, "pd": m.range.premium_discount} if m.range else None,
        "pools_above": [{"price": p.price, "kind": p.kind, "touches": p.touches} for p in m.pools_above[:3]],
        "pools_below": [{"price": p.price, "kind": p.kind, "touches": p.touches} for p in m.pools_below[:3]],
        "sweeps": [{"dir": s.direction, "level": s.level, "kind": s.kind, "age": s.age,
                    "q": round(s.quality, 2), "idm": s.has_inducement} for s in m.sweeps[:3]],
        "zones": len(m.zones), "mitigation": len(m.mitigation_zones),
        "inducements": len(m.inducements), "confluence": m.confluence_score,
        "momentum": f"{m.momentum}({m.momentum_strength:.2f})",
        "trend_dir": f"{m.trend_direction}({m.trend_strength:.2f})",
        "sessions": [{"name": s.name, "swept": s.swept, "dir": s.sweep_dir, "q": s.sweep_quality, "atr": s.range_atr, "killzone": s.killzone, "overlap": s.overlap} for s in m.sessions],
        "cisds": [{"dir": c.direction, "age": c.age, "q": c.quality, "ft": c.follow_through, "after_sw": c.after_sweep, "after_sess": c.after_session} for c in m.cisds],

        "candle": m.candle.patterns, "displacement": m.displacement,
    }


