
"""IMPERION FINANCE AI V17 - Adaptive Trading Policy.

Bounded agent weights and regime-sensitive consensus thresholds.

Public V17 functions preserved:
    check_weights(weights)
    profile(regime_name, volatility=None, adaptive=True, overrides=None)
    effective_weights(learned, prof)

Policy thresholds control decision quality. They do not replace
portfolio risk limits, position sizing, exchange filters or kill switches.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

AGENT_WEIGHTS: Dict[str, float] = {
    "trend": 1.30,
    "structure": 1.20,
    "liquidity": 1.00,
    "zones": 0.90,
    "trigger": 1.00,
    "orderflow": 1.10,
    "microstructure": 0.90,
    "momentum": 0.80,
    "context": 0.60,
    "derivatives": 0.40,
    "external": 0.50,
}

FLOW_AGENTS = (
    "orderflow",
    "microstructure",
    "momentum",
)

LOW_SIGNAL_AGENTS = (
    "external",
    "derivatives",
)

PRICE_AGENTS = (
    "trend",
    "structure",
    "liquidity",
    "zones",
    "trigger",
)

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "min_net": 0.20,
    "min_agree": 4,
    "max_opposition": 0.55,
    "min_playbook": 0.50,
    "htf_veto": 0.60,
    "htf_veto_exempt": 0.70,
}

PROFILES: Dict[str, Dict[str, Dict[str, float]]] = {
    "TREND": {
        "weights": {
            "trend": 1.30,
            "structure": 1.15,
            "momentum": 1.20,
            "liquidity": 0.90,
        },
        "thresholds": {},
    },
    "RANGE": {
        "weights": {
            "trend": 0.60,
            "momentum": 0.70,
            "liquidity": 1.30,
            "zones": 1.20,
            "trigger": 1.10,
        },
        "thresholds": {},
    },
    "TRANSITION": {
        "weights": {
            "trend": 0.85,
            "structure": 1.10,
            "trigger": 1.15,
        },
        "thresholds": {
            "min_net": 0.24,
            "min_playbook": 0.55,
        },
    },
    "VOLATILE": {
        "weights": {
            "microstructure": 0.80,
            "orderflow": 0.90,
            "trigger": 1.10,
        },
        "thresholds": {
            "min_net": 0.28,
            "min_agree": 5,
            "max_opposition": 0.45,
            "min_playbook": 0.58,
        },
    },
}

ALIASES = {
    "TREND_UP": "TREND",
    "TREND_DOWN": "TREND",
}

MIN_WEIGHT = 0.05
MAX_WEIGHT = 5.0

THRESHOLD_BOUNDS = {
    "min_net": (0.0, 1.0),
    "min_agree": (1, len(AGENT_WEIGHTS)),
    "max_opposition": (0.0, 1.0),
    "min_playbook": (0.0, 1.0),
    "htf_veto": (0.0, 1.0),
    "htf_veto_exempt": (0.0, 1.0),
}


class PolicyError(ValueError):
    """Invalid adaptive policy configuration."""


def _number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise PolicyError(f"invalid {name}: {value!r}")
    return float(value)


def _validate_threshold(
    name: str,
    value: Any,
) -> float:
    if name not in THRESHOLD_BOUNDS:
        raise PolicyError(f"unknown threshold {name}")

    number = _number(value, name)
    low, high = THRESHOLD_BOUNDS[name]

    if not low <= number <= high:
        raise PolicyError(
            f"{name} must be between {low} and {high}"
        )

    if name == "min_agree" and not number.is_integer():
        raise PolicyError("min_agree must be an integer")

    return int(number) if name == "min_agree" else number


def check_weights(
    weights: Optional[Mapping[str, float]],
) -> Dict[str, float]:
    """Merge learned weights with bounded baseline weights."""
    out = dict(AGENT_WEIGHTS)

    for name, value in (weights or {}).items():
        if name not in AGENT_WEIGHTS:
            continue

        number = _number(value, f"weight {name}")

        if not MIN_WEIGHT <= number <= MAX_WEIGHT:
            raise PolicyError(
                f"weight {name} outside allowed bounds"
            )

        out[name] = number

    return out


def profile(
    regime_name: Optional[str],
    volatility: Optional[str] = None,
    adaptive: bool = True,
    overrides: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    """Build an explainable trading policy for the current regime."""
    if not adaptive:
        thresholds = dict(DEFAULT_THRESHOLDS)

        for name, value in (overrides or {}).items():
            thresholds[name] = _validate_threshold(name, value)

        return {
            "name": "STATIC",
            "weights": {},
            "thresholds": thresholds,
        }

    key = str(regime_name or "").upper()
    key = ALIASES.get(key, key)

    base = PROFILES.get(key)
    name = key if base else "NEUTRAL"

    thresholds = {
        **DEFAULT_THRESHOLDS,
        **(base["thresholds"] if base else {}),
    }

    if volatility == "EXPANSION" and key != "VOLATILE":
        thresholds["min_net"] = max(
            thresholds["min_net"],
            0.26,
        )
        name += "+EXPANSION"

    for threshold, value in (overrides or {}).items():
        thresholds[threshold] = _validate_threshold(
            threshold,
            value,
        )

    return {
        "name": name,
        "weights": (
            dict(base["weights"]) if base else {}
        ),
        "thresholds": thresholds,
    }


def effective_weights(
    learned: Optional[Mapping[str, float]],
    prof: Mapping[str, Any],
) -> Dict[str, float]:
    """Apply bounded regime multipliers to validated agent weights."""
    weights = check_weights(learned)

    for name, multiplier in (
        prof.get("weights") or {}
    ).items():
        if name not in weights:
            continue

        factor = _number(
            multiplier,
            f"multiplier {name}",
        )

        if factor <= 0:
            raise PolicyError(
                f"invalid multiplier for {name}"
            )

        weights[name] = max(
            MIN_WEIGHT,
            min(MAX_WEIGHT, weights[name] * factor),
        )

    return weights
