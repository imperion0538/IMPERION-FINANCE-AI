"""AI context filter: learns from resolved shadow trades WHERE the strategy loses, and stands aside there.

For every context a setup belongs to (style, style+regime, style+playbook, symbol+side, session, quality bucket) it keeps the
sample size, win rate and expectancy in R. A context vetoes a trade only when it is *proven* negative: enough
samples AND the one-sided 95% upper confidence bound of its expectancy is below zero. The filter can therefore
only remove trades, never add or enlarge one, and it needs no manual promotion. Blocked setups are still
shadow-tracked, so a context that recovers is released again automatically.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

Z_95_ONE_SIDED = 1.645
QUALITY_BUCKETS = ((0, 70), (70, 75), (75, 80), (80, 85), (85, 90), (90, 101))


def quality_bucket(quality: Optional[float]) -> Optional[str]:
    if quality is None:
        return None
    for lo, hi in QUALITY_BUCKETS:
        if lo <= quality < hi:
            return f"{lo}-{hi if hi <= 100 else 100}"
    return None


def context_keys(style: str, regime: Optional[str], symbol: str, side: str, session: Optional[str],
                 quality: Optional[float], playbook: Optional[str] = None) -> List[Tuple[str, str]]:
    keys = [("style", style), ("symbol_side", f"{symbol}|{side}")]
    if playbook:
        keys.append(("style_playbook", f"{style}|{playbook}"))
    if regime and regime != "UNKNOWN":
        keys.append(("style_regime", f"{style}|{regime}"))
    if session:
        keys.append(("style_session", f"{style}|{session}"))
    bucket = quality_bucket(quality)
    if bucket:
        keys.append(("quality", bucket))
    return keys


class ContextStats:
    def __init__(self, samples: List[Dict[str, Any]]):
        """`samples` are resolved shadows (ShadowTracker.resolved()): r_result, style, symbol, side and optional meta."""
        acc: Dict[Tuple[str, str], List[float]] = {}
        for s in samples:
            r = s.get("r_result")
            if r is None:
                continue
            meta = s.get("meta") or {}
            for key in context_keys(s["style"], meta.get("regime"), s["symbol"], s["side"], meta.get("session"), meta.get("quality"),
                                    meta.get("playbook")):
                acc.setdefault(key, []).append(float(r))
        self.n_samples = len(samples)
        self.stats: Dict[Tuple[str, str], Dict[str, float]] = {key: self._stat(rs) for key, rs in acc.items()}

    @staticmethod
    def _stat(rs: List[float]) -> Dict[str, float]:
        n = len(rs)
        mean = sum(rs) / n
        sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
        se = sd / math.sqrt(n) if n > 1 else float("inf")
        return {"n": n, "win_rate": round(sum(1 for r in rs if r > 0) / n, 3), "expectancy_r": round(mean, 4),
                "upper_r": round(mean + Z_95_ONE_SIDED * se, 4) if n > 1 else float("inf")}

    def verdict(self, style: str, regime: Optional[str], symbol: str, side: str, session: Optional[str],
                quality: Optional[float], min_samples: int, playbook: Optional[str] = None) -> Dict[str, Any]:
        matches, reasons = [], []
        for kind, value in context_keys(style, regime, symbol, side, session, quality, playbook):
            st = self.stats.get((kind, value))
            if not st:
                continue
            negative = st["n"] >= min_samples and st["upper_r"] < 0
            matches.append({"context": f"{kind}={value}", "n": st["n"], "win_rate": st["win_rate"], "expectancy_r": st["expectancy_r"],
                            "proven_negative": negative})
            if negative:
                reasons.append(f"AI_CONTEXT_NEGATIVE ({kind}={value}: n={st['n']}, expectancy {st['expectancy_r']:+.2f}R, 95% upper bound {st['upper_r']:+.2f}R)")
        return {"blocked": bool(reasons), "reasons": reasons, "matches": matches}

    def worst(self, min_samples: int, limit: int = 5) -> List[Dict[str, Any]]:
        rows = [{"context": f"{k[0]}={k[1]}", **v} for k, v in self.stats.items() if v["n"] >= min_samples]
        return sorted(rows, key=lambda r: r["expectancy_r"])[:limit]

    def best(self, min_samples: int, limit: int = 5) -> List[Dict[str, Any]]:
        rows = [{"context": f"{k[0]}={k[1]}", **v} for k, v in self.stats.items() if v["n"] >= min_samples]
        return sorted(rows, key=lambda r: -r["expectancy_r"])[:limit]
