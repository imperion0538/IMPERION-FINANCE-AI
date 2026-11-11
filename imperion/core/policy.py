"""Director policy: agent weights, consensus thresholds and how the market regime changes both.

Everything here is a bounded, explainable multiplier. Unknown regimes fall back to the neutral profile.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

AGENT_WEIGHTS: Dict[str, float] = {
    "trend": 1.30, "structure": 1.20, "liquidity": 1.00, "zones": 0.90, "trigger": 1.00,
    "orderflow": 1.10, "microstructure": 0.90, "momentum": 0.80,
    "context": 0.60, "derivatives": 0.40, "external": 0.50,
}
FLOW_AGENTS = ("orderflow", "microstructure", "momentum")
LOW_SIGNAL_AGENTS = ("external", "derivatives")   # contribute to the net score, never to the "enough agents agree" count
PRICE_AGENTS = ("trend", "structure", "liquidity", "zones", "trigger")

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "min_net": 0.20,            # weighted net agreement in the playbook direction
    "min_agree": 4,             # agents that must agree
    "max_opposition": 0.55,     # opposing weight / agreeing weight
    "min_playbook": 0.50,       # playbook completeness
    "htf_veto": 0.60,           # higher-timeframe trend this strong against the trade blocks it ...
    "htf_veto_exempt": 0.70,    # ... unless it is a sweep_reversal at least this complete
}

PROFILES: Dict[str, Dict[str, Dict[str, float]]] = {
    "TREND": {"weights": {"trend": 1.30, "structure": 1.15, "momentum": 1.20, "liquidity": 0.90}, "thresholds": {}},
    "RANGE": {"weights": {"trend": 0.60, "momentum": 0.70, "liquidity": 1.30, "zones": 1.20, "trigger": 1.10}, "thresholds": {}},
    "TRANSITION": {"weights": {}, "thresholds": {"min_net": 0.24}},
    "VOLATILE": {"weights": {"microstructure": 0.80, "orderflow": 0.90},
                 "thresholds": {"min_net": 0.28, "min_agree": 5, "max_opposition": 0.45, "min_playbook": 0.58}},
}
ALIASES = {"TREND_UP": "TREND", "TREND_DOWN": "TREND"}


class PolicyError(ValueError):
    pass


def check_weights(weights: Optional[Mapping[str, float]]) -> Dict[str, float]:
    """Merge learned weights over the base table; unknown agents (e.g. from an older core) are ignored."""
    out = dict(AGENT_WEIGHTS)
    for k, v in (weights or {}).items():
        if k not in AGENT_WEIGHTS:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            raise PolicyError(f"invalid weight for {k}: {v!r}")
        out[k] = float(v)
    return out


def profile(regime_name: Optional[str], volatility: Optional[str] = None, adaptive: bool = True,
            overrides: Optional[Mapping[str, float]] = None) -> Dict[str, Any]:
    """`overrides` replace thresholds after the regime adjustment (sensitivity analysis / research only)."""
    if not adaptive:
        return {"name": "STATIC", "weights": {}, "thresholds": {**DEFAULT_THRESHOLDS, **(overrides or {})}}
    key = ALIASES.get(str(regime_name or "").upper(), str(regime_name or "").upper())
    base = PROFILES.get(key)
    name = key if base else "NEUTRAL"
    th = {**DEFAULT_THRESHOLDS, **(base["thresholds"] if base else {})}
    if volatility == "EXPANSION" and key != "VOLATILE":
        th["min_net"] = max(th["min_net"], 0.26)
        name += "+EXPANSION"
    for k, v in (overrides or {}).items():
        if k not in DEFAULT_THRESHOLDS:
            raise PolicyError(f"unknown threshold {k}")
        th[k] = v
    return {"name": name, "weights": dict(base["weights"]) if base else {}, "thresholds": th}


def effective_weights(learned: Optional[Mapping[str, float]], prof: Mapping[str, Any]) -> Dict[str, float]:
    w = check_weights(learned)
    for k, m in (prof.get("weights") or {}).items():
        if k in w:
            w[k] = max(0.05, w[k] * float(m))
    return w
