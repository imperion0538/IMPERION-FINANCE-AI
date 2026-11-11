"""Parameter set for automatic optimization of price-action and Director thresholds.

All discretionary constants that affect playbook scoring and decision gates
are collected here so they can be proposed, tested and versioned.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Dict, Any


@dataclass
class ParameterSet:
    # Playbook age / position thresholds
    sweep_max_age_setup: int = 6
    sweep_max_age_trigger: int = 10
    trigger_break_max_age: int = 12
    pullback_max_pos: float = 0.62
    ote_lo: float = 0.21
    ote_hi: float = 0.382
    retest_max_age: int = 30
    range_edge: float = 0.18

    # Confirmation minima
    sweep_conf_min: float = 0.45
    retest_conf_min: float = 0.40
    pullback_conf_min: float = 0.30

    # Director thresholds (from policy.DEFAULT_THRESHOLDS)
    min_playbook: float = 0.50
    min_net: float = 0.20
    min_agree: int = 4
    max_opposition: float = 0.55
    htf_veto: float = 0.60
    htf_veto_exempt: float = 0.70

    # Score weight multipliers (relative; will be normalized in use)
    w_sweep_quality: float = 0.36
    w_sweep_conf: float = 0.30
    w_pullback_impulse: float = 0.20
    w_pullback_zone: float = 0.25
    w_pullback_conf: float = 0.30

    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("meta", None)
        return d

    def threshold_overrides(self) -> Dict[str, float]:
        """Subset used by existing Director / policy."""
        return {
            "min_playbook": self.min_playbook,
            "min_net": self.min_net,
            "min_agree": float(self.min_agree),
            "max_opposition": self.max_opposition,
            "htf_veto": self.htf_veto,
            "htf_veto_exempt": self.htf_veto_exempt,
        }

    @classmethod
    def default(cls) -> "ParameterSet":
        return cls()

    def clone(self, **changes) -> "ParameterSet":
        d = self.to_dict()
        d.update(changes)
        return ParameterSet(**d)
