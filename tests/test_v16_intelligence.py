from datetime import datetime, timezone

from imperion.analysis.priceaction.confluence import adaptive_thresholds, candle_flow_divergence, displacement_quality, smt_modifier
from imperion.analysis.priceaction.market_models import detect_mmbm_candidate
from imperion.analysis.priceaction.mapper import build_map
from imperion.analysis.priceaction.sessions import killzone_at
from imperion.models import Candle, BULLISH, BEARISH
from .helpers import make_candles


def _ts(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_killzone_uses_dst_aware_local_clock_and_overlap():
    # 2026-01-15 14:00 UTC = 09:00 New York and 14:00 London: actual overlap.
    label, overlap = killzone_at(_ts("2026-01-15T14:00:00"))
    assert label == "LONDON_NY_OVERLAP"
    assert overlap is True
    # Summer overlap shifts one hour earlier in UTC.
    label, overlap = killzone_at(_ts("2026-07-15T13:00:00"))
    assert label == "LONDON_NY_OVERLAP"
    assert overlap is True


def test_displacement_quality_is_bounded_and_explainable():
    candles = make_candles([100 + i * 0.1 for i in range(30)])
    result = displacement_quality(candles, atr=0.2)
    assert 0.0 <= result["score"] <= 1.0
    assert {"body_ratio", "atr_multiple", "volume_ratio", "continuity"} <= set(result)


def test_smt_modifier_is_bounded_and_directional():
    assert smt_modifier({"available": True, "direction": BULLISH}, BULLISH)[0] == 0.10
    assert smt_modifier({"available": True, "direction": BEARISH}, BULLISH)[0] == -0.08
    assert smt_modifier({"available": False}, BULLISH)[0] == 0.0


def test_adaptive_thresholds_tighten_in_high_volatility():
    normal = adaptive_thresholds("NORMAL")
    volatile = adaptive_thresholds("VOLATILE")
    assert volatile["idm_min_quality"] > normal["idm_min_quality"]
    assert volatile["displacement_min"] > normal["displacement_min"]
    assert volatile["ote_tolerance_atr"] < normal["ote_tolerance_atr"]


def test_mapper_populates_nested_structure_and_displacement_quality():
    closes = [100 + i * 0.08 + (0.5 if i % 7 < 3 else -0.4) for i in range(360)]
    result = build_map(make_candles(closes))
    assert result.ok
    assert result.structure.external_trend in ("UP", "DOWN", "RANGE", "UNKNOWN")
    assert result.structure.internal_trend in ("UP", "DOWN", "RANGE", "UNKNOWN")
    assert 0.0 <= result.displacement_quality <= 1.0


def test_mmbm_candidate_is_research_only():
    candles = make_candles([100 + (i % 4) * 0.05 for i in range(80)])
    result = detect_mmbm_candidate(candles, atr=0.5)
    assert result.get("available") is True
    if result.get("candidate"):
        assert result["research_only"] is True



def test_candle_delta_divergence_reports_proxy_source():
    candles = make_candles([100 + (i * 0.05) for i in range(20)], buy_fracs=[0.5] * 20)
    result = candle_flow_divergence(candles)
    assert result["available"] is True
    assert result["source"] == "candle_delta_proxy"
    assert result["direction"] in ("NONE", "BULLISH", "BEARISH", "CONFLICT")
