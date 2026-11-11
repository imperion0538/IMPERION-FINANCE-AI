"""IMPERION FINANCE AI V17 — complete playbook replacement.

Eight setup models, bounded confluence and candidate geometry validation.

Scores describe setup completeness, not win probability.
This module generates candidates; it never executes orders.
Final stop buffers, targets, costs and style-specific risk/reward
are evaluated by the existing planner.
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


def _p(params
