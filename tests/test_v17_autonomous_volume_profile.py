"""AUTONOMOUS PRO 1.5 RL Volume Profile / OB integration with V17.

Confirms profiles are real source-derived logic, are serialized into V17 PA maps,
contribute only a bounded confluence bonus, and cannot silently override stops.
"""

from dataclasses import replace
import math

import pytest

from imperion.analysis.priceaction.mapper import build_map, summary
from imperion.analysis.priceaction.types import PAMap, Playbook, Pool, Zone
from imperion.analysis.priceaction import playbooks
from imperion.analysis.priceaction.volume_profile import (
    Session, VolumeProfile, build_volume_profile, build_session_profiles,
    build_killzone_session_vp, order_block_vp_confluence,
    refine_entry_stop_with_vp, volume_profile_bias,
)
from imperion.config import STYLES, Settings
from imperion.core.planner import build_plan
from imperion.models import BEARISH, BULLISH, Candle
from .helpers import make_candles, random_walk


def bars(n=64, start=1_704_067_200_000):
    out = []
    for i in range(n):
        base = 100.0 + (0.01 if i % 3 else -0.01)
        out.append(Candle(t=start + i * 900_000, o=base, h=100.25, l=99.75,
                          c=100.03, v=200.0 if i < n//2 else 110.0,
                          ct=start + (i + 1) * 900_000 - 1))
    return out


def test_profile_uses_volume_weighted_price_bins():
    profile = build_volume_profile(bars(), bins=36)
    assert profile is not None
    assert profile.total_volume == pytest.approx(32*200+32*110)
    assert 99.75 < profile.poc < 100.25
    assert profile.val <= profile.poc <= profile.vah
    assert sum(node.volume for node in profile.nodes) == pytest.approx(profile.total_volume)
    assert profile.session is None


def test_profile_rejects_invalid_or_insufficient_inputs():
    assert build_volume_profile(bars(7)) is None
    assert build_volume_profile(bars(), bins=0) is None
    assert build_volume_profile(bars(), value_area_pct=1.5) is None
    bad = bars()
    bad[-1] = replace(bad[-1], v=math.nan)
    assert build_volume_profile(bad) is None


def test_session_and_killzone_profiles_are_separate():
    raw = bars(160)
    by_session = build_session_profiles(raw)
    by_kz = build_killzone_session_vp(raw)
    assert set(by_session) == {"ASIA", "LONDON", "NEW_YORK", "COMPOSITE"}
    assert set(by_kz) == {"ASIA_KZ", "LONDON_KZ", "NY_KZ"}
    assert by_session["COMPOSITE"] is not None
    assert any(v is not None for v in by_kz.values())


def test_ob_hvn_value_area_poc_scoring_and_invalidated_filter():
    vp = VolumeProfile(poc=100, vah=101, val=99, total_volume=1000,
                       hvn_levels=[100], lvn_levels=[105])
    bull = Zone("OB", BULLISH, 99.8, 100.2, 40, quality=0.50)
    bear = Zone("OB", BEARISH, 103.0, 104.0, 42, quality=0.50, state="INVALIDATED")
    fvg = Zone("FVG", BULLISH, 99.8, 100.2, 40, quality=0.80)
    result = order_block_vp_confluence([bull, bear, fvg], vp, price=100, atr=1)
    assert len(result) == 1
    assert result[0]["vp_score"] == pytest.approx(0.78)
    assert result[0]["reasons"] == ["HVN", "POC"]
    assert result[0]["preferred"] is True
    assert volume_profile_bias(vp, 100, "UP") >= 0


def test_refinement_cannot_change_v17_executable_geometry():
    vp = VolumeProfile(poc=99.4, vah=101, val=98.4, total_volume=1000,
                       hvn_levels=[99.4], lvn_levels=[])
    entry, stop, note = refine_entry_stop_with_vp(BULLISH, 100, 98, vp, atr=2)
    assert note
    # These are reference prices only; never feed them into the executable V17 plan.
    assert entry != 100 or stop != 98
    assert refine_entry_stop_with_vp("NEUTRAL", 100, 98, vp, 2) == (100, 98, "")


def test_priceaction_maps_include_vp_ohlcv_evidence():
    cs = make_candles(random_walk(320, 21))
    pa = build_map(cs)
    data = summary(pa)
    assert pa.ok and data["volume_profile"]["source"] == "OHLCV_uniform_bin_estimate"
    assert len(data["vp_sessions"]) == 4
    assert len(data["vp_killzones"]) == 3
    assert isinstance(data["vp_ob_confluence"], list)
    assert all("vp_score" in item and "direction" in item for item in data["vp_ob_confluence"])


def test_playbook_bonus_is_bounded_directional_and_requires_nearby_preferred_ob(monkeypatch):
    for name in ("sweep_reversal", "trend_pullback", "breakout_retest", "range_fade",
                 "inducement_sweep", "ote_pullback", "mitigation_retest", "session_cisd"):
        def producer(*args, _name=name, **kwargs):
            return [Playbook("sweep_reversal", "LONG", 0.65, [], 98, 106)] if _name == "sweep_reversal" else []
        monkeypatch.setattr(playbooks, name, producer)
    monkeypatch.setattr(playbooks, "filter_valid_playbooks", lambda rows, *args, **kwargs: rows)

    plain = PAMap(ok=True, price=100.0, atr=2.0)
    row = {"direction": BULLISH, "mid": 99.5, "vp_score": 0.83,
           "base_quality": 0.6, "preferred": True,
           "reasons": ["HVN"], "distance_atr": 0.25}
    positive = PAMap(ok=True, price=100.0, atr=2.0, vp_ob_confluence=[row])
    distant = PAMap(ok=True, price=100.0, atr=2.0,
                    vp_ob_confluence=[dict(row, distance_atr=1.501)])
    opposing = PAMap(ok=True, price=100.0, atr=2.0,
                     vp_ob_confluence=[dict(row, direction=BEARISH)])
    value = lambda m: playbooks.find_playbooks(m, PAMap(ok=False), None, "UNKNOWN")[0]
    assert value(positive).score - value(plain).score == pytest.approx(0.04)
    assert value(distant).score == value(plain).score
    assert value(opposing).score == value(plain).score
    assert "OB+Volume Profile" in " ".join(value(positive).steps)


def test_v17_planner_never_uses_vp_reference_as_live_entry_stop():
    cfg = Settings()
    pb = Playbook("test", "LONG", 0.80, invalidation=98.5, target=106)
    reference = {"poc": 99.4, "val": 98.4, "vah": 101.0,
                 "volume": 10000, "hvn": [99.4], "lvn": []}
    vp_row = {"direction": BULLISH, "preferred": True,
              "distance_atr": 0.2, "vp_score": 0.85, "reasons": ["HVN"]}
    # Match sufficient target liquidity so the V17 plan is valid without any VP changes.
    baseline = PAMap(ok=True, price=100, atr=1,
                     pools_above=[Pool(price=104, kind="EQH", touches=2, last_i=12)])
    enhanced = PAMap(ok=True, price=100, atr=1,
                     pools_above=baseline.pools_above,
                     volume_profile=reference, vp_ob_confluence=[vp_row])
    original, why, facts = build_plan("BTCUSDT", STYLES["day"], pb, baseline, cfg)
    updated, other, detail = build_plan("BTCUSDT", STYLES["day"], pb, enhanced, cfg)
    assert why == other == []
    assert original is not None and updated is not None
    assert (updated.entry, updated.stop, updated.tp1, updated.tp2) == (
        original.entry, original.stop, original.tp1, original.tp2
    )
    assert detail["vp_ob_reference"]["executed"] is False
    assert detail["vp_ob_reference"]["score"] == 0.85
