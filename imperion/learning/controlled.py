"""
Controlled AI learning validation.
Suggestions are recorded; they are never auto-promoted to live weights.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping


def validate_learning(suggestion: Mapping[str, Any], min_samples: int = 30, min_win_rate: float = 0.45) -> Dict[str, Any]:
    samples = int(suggestion.get("samples") or suggestion.get("n") or 0)
    win_rate = suggestion.get("win_rate")
    reasons = []
    if samples < min_samples:
        reasons.append(f"NOT_ENOUGH_SAMPLES ({samples}<{min_samples})")
    if win_rate is None:
        reasons.append("WIN_RATE_UNKNOWN")
    elif float(win_rate) < min_win_rate:
        reasons.append(f"WIN_RATE_TOO_LOW ({float(win_rate):.2f})")
    return {
        "approved_for_promotion": not reasons,
        "auto_applied": False,
        "reasons": reasons or ["MANUAL_APPROVAL_STILL_REQUIRED"],
        "samples": samples,
        "win_rate": win_rate,
        "note": "Learning results require explicit human promotion.",
    }


def summarize_shadows(shadows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    closed = [s for s in shadows if s.get("outcome") in ("win", "loss", "flat")]
    wins = [s for s in closed if s.get("outcome") == "win"]
    return {
        "samples": len(closed),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "status": "ready_for_review" if len(closed) >= 30 else "collecting",
    }
