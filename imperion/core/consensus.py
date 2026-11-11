"""Agent consensus: how strongly, how broadly and how unopposed the team agrees with one direction."""
from __future__ import annotations

from typing import Any, Dict, Mapping

from ..models import NEUTRAL, AgentReport
from .policy import FLOW_AGENTS, LOW_SIGNAL_AGENTS, PRICE_AGENTS


def consensus(reports: Mapping[str, AgentReport], weights: Mapping[str, float], direction: str,
              thresholds: Mapping[str, float]) -> Dict[str, Any]:
    num = den = agree_w = oppose_w = 0.0
    agree, oppose, neutral = [], [], []
    for name, r in reports.items():
        w = weights.get(name)
        if not r.available or not w:
            continue
        if r.direction == NEUTRAL or r.confidence < 0.05:
            den += 0.5 * w                      # looked and saw nothing: dilutes, does not oppose
            neutral.append(name)
            continue
        sign = 1.0 if r.direction == direction else -1.0
        num += sign * w * r.confidence
        den += w
        if sign > 0:
            agree.append(name)
            agree_w += w * r.confidence
        else:
            oppose.append(name)
            oppose_w += w * r.confidence
    net = num / den if den else 0.0
    opposition = oppose_w / agree_w if agree_w else (1.0 if oppose_w else 0.0)
    reasons = []
    if net < thresholds["min_net"]:
        reasons.append(f"WEAK_CONSENSUS (net {net:+.2f} < {thresholds['min_net']:.2f})")
    core_agree = [a for a in agree if a not in LOW_SIGNAL_AGENTS]
    if len(core_agree) < thresholds["min_agree"]:
        reasons.append(f"TOO_FEW_AGENTS ({len(core_agree)} < {int(thresholds['min_agree'])}: {', '.join(sorted(core_agree)) or '-'})")
    if opposition > thresholds["max_opposition"]:
        reasons.append(f"OPPOSITION ({opposition:.2f} > {thresholds['max_opposition']:.2f}: {', '.join(sorted(oppose))})")
    if not any(a in PRICE_AGENTS for a in agree):
        reasons.append("NO_PRICE_ACTION_AGREEMENT")
    return {"direction": direction, "net": round(net, 4), "agree": sorted(agree), "oppose": sorted(oppose), "neutral": sorted(neutral),
            "opposition": round(opposition, 3), "flow_confirmed": any(a in FLOW_AGENTS for a in agree),
            "passed": not reasons, "reasons": reasons}
