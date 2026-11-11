"""Price-action engine: structure state machine, liquidity, zones, candle patterns, playbooks."""
import pytest

from imperion.analysis.indicators import atr_series
from imperion.analysis.priceaction import build_map, summary, smt_divergence
from imperion.analysis.priceaction.candles import candle_signal
from imperion.analysis.priceaction.liquidity import find_pools, find_sweeps
from imperion.analysis.priceaction.playbooks import (REGIME_FIT, breakout_retest, confirmation, find_playbooks, range_fade,
                                                     sweep_reversal, trend_pullback)
from imperion.analysis.priceaction.structure import analyze_structure, dealing_range, find_swings
from imperion.analysis.priceaction.types import (Break, CandleSignal, DealingRange, PAMap, Pool, Structure, Sweep, Zone)
from imperion.analysis.priceaction.zones import find_zones, polarity, zone_quality, zones_at_price
from imperion.models import BEARISH, BULLISH, NEUTRAL

from .helpers import append, bar, make_candles, random_walk, trend, zigzag

UP = [100, 106, 103, 109, 106, 112, 109, 115, 112, 118, 115, 121, 118, 124]


def structure_of(cs):
    return analyze_structure(cs, find_swings(cs), atr_series(cs))


# ---- structure -----------------------------------------------------------------------------------------
def test_uptrend_is_read_as_bos_up_with_higher_highs_and_lows():
    st = structure_of(zigzag(UP))
    assert st.trend == "UP" and st.swing_bias == "HH_HL"
    assert st.last_break.direction == "UP" and st.last_break.kind == "BOS"
    assert all(b.direction == "UP" for b in st.breaks[1:])
    assert st.protected is not None and st.protected < st.last_break.level


def test_first_break_against_the_trend_is_a_change_of_character():
    cs = zigzag(UP + [100], steps=6)
    st = structure_of(cs)
    assert st.trend == "DOWN" and st.last_break.kind == "CHOCH" and st.last_break.direction == "DOWN"


def test_two_quick_changes_of_character_mean_range():
    cs = zigzag(UP + [100, 126, 98], steps=6)                 # down through the lows, up through the highs, down again
    st = structure_of(cs)
    assert [b.kind for b in st.breaks[-2:]] == ["CHOCH", "CHOCH"] and st.trend == "RANGE"


def test_swings_are_only_used_after_confirmation():
    cs = zigzag([100, 106, 103], steps=6)              # high at 106 needs 3 candles after it before it can be broken
    n = len(cs)
    append(cs, 103, 106.6, 102.9, 106.5)                # closes above 106 ...
    st = structure_of(cs)
    assert st.last_break is not None and st.last_break.level == pytest.approx(106 * 1.0005, rel=1e-3)
    assert st.last_break.index == n                     # ... and is a break because the swing was confirmed before


def test_dealing_range_follows_the_impulse_leg_in_a_trend():
    cs = zigzag(UP + [121], steps=6)
    st = structure_of(cs)
    a = atr_series(cs)[-1]
    rng = dealing_range(cs, st, a)
    assert rng.source == "leg" and rng.low == pytest.approx(st.protected) and 0.0 < rng.position < 1.0


# ---- liquidity -----------------------------------------------------------------------------------------
def _equal_lows_then_sweep(reclaim_same_candle=True):
    cs = zigzag([102, 100, 103, 100.02, 103.5, 100.01, 103, 101.5], steps=5)
    lvl = min(c.l for c in cs)
    if reclaim_same_candle:
        append(cs, 101.5, 101.6, lvl - 0.6, lvl + 0.4)                  # wick through, close back above
    else:
        append(cs, 101.5, 101.6, lvl - 0.6, lvl - 0.3)                  # closes below ...
        append(cs, lvl - 0.3, lvl + 0.6, lvl - 0.35, lvl + 0.5, v=300, bf=0.8)   # ... and is reclaimed by the next candle
    return cs, lvl


@pytest.mark.parametrize("same", [True, False])
def test_sell_side_sweep_of_equal_lows_is_bullish(same):
    cs, lvl = _equal_lows_then_sweep(same)
    a = atr_series(cs)[-1]
    sweeps = find_sweeps(cs, find_swings(cs, 2, 2), a)
    best = [s for s in sweeps if s.direction == BULLISH]
    assert best and best[0].kind in ("EQL", "LOW") and best[0].level == pytest.approx(lvl, rel=2e-3)
    assert best[0].extreme < lvl and 0.5 <= best[0].quality <= 0.85


def test_a_close_below_without_reclaim_is_not_a_sweep():
    cs, lvl = _equal_lows_then_sweep(True)
    cs.pop()
    append(cs, 101.5, 101.6, lvl - 0.6, lvl - 0.4)
    append(cs, lvl - 0.4, lvl - 0.2, lvl - 0.9, lvl - 0.8)
    a = atr_series(cs)[-1]
    assert not [s for s in find_sweeps(cs, find_swings(cs, 2, 2), a) if s.direction == BULLISH]


def test_pools_are_untouched_levels_on_each_side():
    cs = zigzag([100, 104, 101, 104.02, 101.5, 103], steps=5)
    a = atr_series(cs)[-1]
    above, below = find_pools(cs, find_swings(cs, 2, 2), a)
    assert above and above[0].price >= 104 and above[0].kind in ("EQH", "HIGH")
    assert below and all(p.price < cs[-1].c for p in below)
    assert [p.price for p in above] == sorted(p.price for p in above)


# ---- zones ---------------------------------------------------------------------------------------------
def _ob_series():
    cs = [bar(i, 100, 100.4, 99.6, 100 + (0.1 if i % 2 else -0.1)) for i in range(30)]
    append(cs, 100.1, 100.2, 99.5, 99.6)                                     # last bearish candle = order block
    append(cs, 99.6, 102.6, 99.55, 102.5, v=400, bf=0.85)                   # displacement up through the range
    for _ in range(5):
        append(cs, 102.5, 102.8, 102.3, 102.6)
    return cs


def test_order_block_flips_into_a_breaker_and_dies_when_reclaimed():
    cs = _ob_series()
    zones = [z for z in find_zones(cs, atr_series(cs)) if z.kind == "OB"]
    ob = zones[0]
    assert ob.direction == BULLISH and polarity(ob) == BULLISH and ob.lo == pytest.approx(99.5)
    append(cs, 102.6, 102.6, 99.0, 99.2)                                      # closes through the OB low -> bearish breaker
    flipped = [z for z in find_zones(cs, atr_series(cs)) if z.kind == "OB" and z.created == ob.created]
    assert flipped and flipped[0].flipped_at is not None and polarity(flipped[0]) == BEARISH
    append(cs, 99.2, 100.6, 99.1, 100.5)                                      # closes back above the zone -> dead
    assert not [z for z in find_zones(cs, atr_series(cs)) if z.kind == "OB" and z.created == ob.created]


def test_fvg_tracks_fill_and_zone_quality_prefers_fresh_zones():
    cs = _ob_series()
    fvg = [z for z in find_zones(cs, atr_series(cs)) if z.kind == "FVG" and z.direction == BULLISH][0]
    assert fvg.lo < fvg.hi and fvg.fill == 0.0
    mid = (fvg.lo + fvg.hi) / 2
    append(cs, 102.6, 102.7, mid, 102.4)
    again = [z for z in find_zones(cs, atr_series(cs)) if z.kind == "FVG" and z.created == fvg.created][0]
    assert 0.4 <= again.fill <= 0.6 and again.touches == 1
    fresh = Zone("OB", BULLISH, 1, 2, 0, touches=0, displacement=2.0)
    used = Zone("OB", BULLISH, 1, 2, 0, touches=3, displacement=2.0)
    assert zone_quality(fresh) > zone_quality(used)
    assert zones_at_price([fresh], 2.1, 1.0, BULLISH) and not zones_at_price([fresh], 2.1, 1.0, BEARISH)


# ---- candles -------------------------------------------------------------------------------------------
def test_candle_patterns():
    base = [bar(i, 100, 100.5, 99.5, 100.2 if i % 2 else 99.8) for i in range(10)]
    eng = base + [bar(10, 100.2, 100.3, 99.4, 99.5), bar(11, 99.4, 100.9, 99.3, 100.8)]
    assert candle_signal(eng, 1.0).direction == BULLISH and "bullish engulfing" in candle_signal(eng, 1.0).patterns
    pin = base + [bar(10, 100, 100.3, 98.6, 100.1), bar(11, 100.1, 100.3, 98.0, 100.15)]
    assert candle_signal(pin, 1.0).direction == BULLISH
    star = base + [bar(10, 101.5, 101.6, 100.0, 100.1), bar(11, 100.1, 100.2, 99.9, 100.05), bar(12, 100.05, 101.2, 100.0, 101.1)]
    assert "morning star" in candle_signal(star, 1.0).patterns
    assert candle_signal(base, 1.0).direction == NEUTRAL and candle_signal(base[:2], 1.0).direction == NEUTRAL


# ---- whole map -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(5))
def test_map_is_total_on_random_markets(seed):
    m = build_map(make_candles(random_walk(320, seed)))
    s = summary(m)
    assert m.ok and s["trend"] in ("UP", "DOWN", "RANGE", "UNKNOWN") and m.atr > 0
    for p in m.pools_above:
        assert p.price > m.price
    for p in m.pools_below:
        assert p.price < m.price
    for sw in m.sweeps:
        assert 0.5 <= sw.quality <= 0.85 and sw.direction in (BULLISH, BEARISH)
    assert not build_map(make_candles(random_walk(30, seed))).ok


def test_map_reads_trends():
    assert build_map(make_candles(trend(320, drift=0.0025))).structure.trend == "UP"
    assert build_map(make_candles(trend(320, drift=-0.0025))).structure.trend == "DOWN"


# ---- playbooks (hand-built maps: every rule is exercised deterministically) -----------------------------
def pmap(price=100.0, atr=1.0, **kw) -> PAMap:
    base = dict(ok=True, n=300, price=price, atr=atr, structure=Structure(trend="RANGE"), range=DealingRange(110, 90, (price - 90) / 20, "window"),
                recent_low=price - 0.5, recent_high=price + 0.5)
    base.update(kw)
    return PAMap(**base)


def trig(direction=BULLISH, kind="CHOCH", age=2, candle=True) -> PAMap:
    st = Structure(trend="UP" if direction == BULLISH else "DOWN",
                   last_break=Break(kind, "UP" if direction == BULLISH else "DOWN", 100.0, 290, 1.5, age=age))
    return pmap(structure=st, candle=CandleSignal(direction, 0.62, ["engulfing"]) if candle else CandleSignal())


def test_confirmation_combines_trigger_break_and_candle():
    c, steps = confirmation(PAMap(ok=False), trig(BULLISH), BULLISH)
    assert c > 0.75 and len(steps) == 2
    assert confirmation(PAMap(ok=False), trig(BULLISH), BEARISH)[0] == 0.0


def test_sweep_reversal_needs_a_sweep_and_a_shift():
    setup = pmap(price=100.5, sweeps=[Sweep(BULLISH, 100.0, 99.4, 298, 1, 2, "EQL", 0.74)], pools_above=[Pool(106.0, "EQH", 2, 250)],
                 range=DealingRange(110, 99.4, 0.1, "window"))
    pb = sweep_reversal(setup, trig(BULLISH), None)
    assert len(pb) == 1 and pb[0].side == "LONG" and pb[0].invalidation == 99.4 and pb[0].target == 106.0 and pb[0].score >= 0.55
    assert not sweep_reversal(setup, pmap(), None), "a sweep without a lower-timeframe shift is only a wick"
    against = sweep_reversal(setup, trig(BULLISH), {"direction": BEARISH, "strength": 0.9})[0]
    assert against.score < pb[0].score


def test_trend_pullback_into_a_fresh_zone():
    st = Structure(trend="UP", last_break=Break("BOS", "UP", 104.0, 270, 1.8, age=12), protected=98.0, protected_i=250, swing_bias="HH_HL")
    zone = Zone("OB", BULLISH, 100.0, 101.0, 260, touches=0, displacement=2.0)
    setup = pmap(price=100.8, structure=st, range=DealingRange(108.0, 98.0, 0.28, "leg"), zones=[zone], pools_above=[Pool(108.2, "HIGH", 1, 268)])
    pb = trend_pullback(setup, trig(BULLISH, "BOS"), {"direction": BULLISH, "strength": 0.7})
    assert len(pb) == 1 and pb[0].side == "LONG" and pb[0].invalidation == 100.0 and pb[0].target == 108.0 and pb[0].score > 0.6
    assert not trend_pullback(setup, trig(BULLISH), {"direction": BEARISH, "strength": 0.8}), "no pullback buying into a bearish HTF"
    chased = pmap(price=107.5, structure=st, range=DealingRange(108.0, 98.0, 0.95, "leg"), zones=[zone])
    assert not trend_pullback(chased, trig(BULLISH), None), "no entry at the top of the leg"


def test_breakout_retest_holds_the_broken_level():
    st = Structure(trend="UP", last_break=Break("BOS", "UP", 100.0, 290, 1.6, major=True, age=6))
    setup = pmap(price=100.4, structure=st, recent_low=100.1, pools_above=[Pool(104, "HIGH", 1, 200)])
    pb = breakout_retest(setup, trig(BULLISH, "BOS"), None)
    assert len(pb) == 1 and pb[0].side == "LONG" and pb[0].invalidation == pytest.approx(99.7)
    lost = pmap(price=99.5, structure=st, recent_low=99.2)
    assert not breakout_retest(lost, trig(BULLISH), None)


def test_range_fade_only_in_ranges_and_capped_at_the_edge():
    setup = pmap(price=90.8, range=DealingRange(110, 90, 0.04, "window"), recent_low=90.2)
    pb = range_fade(setup, trig(BULLISH), "RANGE")
    assert len(pb) == 1 and pb[0].side == "LONG" and pb[0].cap == 110 and pb[0].invalidation == 90
    trending = pmap(price=90.8, structure=Structure(trend="DOWN"), range=DealingRange(110, 90, 0.04, "window"))
    assert not range_fade(trending, trig(BULLISH), "TREND_DOWN")
    assert REGIME_FIT["TREND_UP"]["range_fade"] == 0.0
    assert not [p for p in find_playbooks(setup, trig(BULLISH), None, "TREND_UP") if p.name == "range_fade"]
    all_pb = find_playbooks(setup, trig(BULLISH), None, "RANGE")
    assert all_pb == sorted(all_pb, key=lambda p: -p.score)


def test_smt_divergence_uses_confirmed_correlated_swings_only():
    primary = zigzag([100, 110, 103, 112, 104, 115, 105, 117, 106, 119, 108, 109], steps=4)
    comparison = zigzag([100, 110, 103, 111, 104, 110, 105, 109, 106, 108, 107, 108], steps=4)
    # Add right-side bars so the final pivots are confirmed, not guessed early.
    append(primary, 109, 110, 108, 109.5)
    append(primary, 109.5, 110, 109, 109.7)
    append(primary, 109.7, 110, 109.2, 109.8)
    append(comparison, 108, 109, 107.5, 108.2)
    append(comparison, 108.2, 109, 108, 108.5)
    append(comparison, 108.5, 109, 108.1, 108.6)
    rep = smt_divergence(primary, comparison)
    assert rep["available"] and rep["confirmed_only"]
    assert rep["direction"] in ("BEARISH", "CONFLICT", "NONE")
    assert smt_divergence(primary[:5], comparison[:5])["available"] is False


def test_smt_rejects_unaligned_time_series():
    primary = zigzag([100, 105, 101, 106, 102, 107, 103, 108], steps=4)
    comparison = zigzag([100, 105, 101, 106, 102, 107, 103, 108], steps=4, t0=1_800_000_000_000)
    assert smt_divergence(primary, comparison)["reason"] == "unaligned_series"
