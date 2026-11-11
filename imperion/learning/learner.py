"""Agent-weight learner: proposes new agent weights from labelled shadow outcomes. Never applies them itself.

A bounded, auditable re-weighting: minimum sample size, shrinkage towards the base weights, per-update change cap,
absolute bounds, chronological train/validation split, versioned weights with manual approval and rollback.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Tuple

from ..config import Settings
from ..core.consensus import consensus
from ..core.policy import AGENT_WEIGHTS, DEFAULT_THRESHOLDS, check_weights
from ..db import Database
from ..models import DIR_OF, AgentReport
from .performance import performance_report
from .shadow import ShadowTracker
from .walk_forward import make_folds

log = logging.getLogger("imperion.learner")

SHRINK_K = 20.0
STEP_CAP = 0.10          # max relative change per update vs the active weight
ABS_LOW, ABS_HIGH = 0.5, 1.5   # bounds relative to the BASE weight
WF_SPLITS = 4
MIN_EXPECTANCY_GAIN = 0.05


def _reports(snap: Dict[str, Dict[str, Any]]) -> Dict[str, AgentReport]:
    return {k: AgentReport(k, v["d"], float(v["s"])) for k, v in snap.items() if k in AGENT_WEIGHTS}


class WeightLearner:
    def __init__(self, db: Database, cfg: Settings, shadows: ShadowTracker):
        self.db, self.cfg, self.shadows = db, cfg, shadows

    # ---- weights registry ---------------------------------------------
    @staticmethod
    def _ours(row: Dict[str, Any]) -> bool:
        return bool(set(row.get("weights") or {}) & set(AGENT_WEIGHTS))      # rows of older engines are ignored

    def active(self) -> Tuple[int, Dict[str, float]]:
        rows = [r for r in self.db.weights_by_status("active") if self._ours(r)]
        if rows:
            return rows[0]["version"], check_weights(rows[0]["weights"])
        return 0, dict(AGENT_WEIGHTS)

    def proposals(self) -> List[Dict[str, Any]]:
        return [r for r in self.db.weights_by_status("proposed") if self._ours(r)]

    def approve(self, version: int) -> bool:
        row = next((r for r in self.proposals() if r["version"] == version), None)
        if not row:
            return False
        for r in self.db.weights_by_status("active"):
            self.db.set_weights_status(r["version"], "retired")
        self.db.set_weights_status(version, "active")
        self.db.event("WARNING", "ai_weights_activated", f"AI weights v{version} activated")
        return True

    def reject(self, version: int) -> None:
        self.db.set_weights_status(version, "rejected")

    def rollback(self) -> int:
        """Retire the active weights and return to the base weights (version 0)."""
        for r in self.db.weights_by_status("active"):
            self.db.set_weights_status(r["version"], "retired")
        self.db.event("WARNING", "ai_weights_rollback", "AI weights rolled back to base")
        return 0

    # ---- statistics -----------------------------------------------------
    @staticmethod
    def edges(samples: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
        acc: Dict[str, List[float]] = {}
        for s in samples:
            side = 1 if s["side"] == "LONG" else -1
            for eng, v in s["evidence"].items():
                if eng not in AGENT_WEIGHTS or v["d"] == "NEUTRAL" or v["s"] <= 0:
                    continue
                a = (1 if v["d"] == "BULLISH" else -1) * side * v["s"]
                st = acc.setdefault(eng, [0.0, 0.0, 0.0])    # sum(a*R), sum|a|, n
                st[0] += a * s["r_result"]; st[1] += abs(a); st[2] += 1
        out = {}
        for eng, (num, den, n) in acc.items():
            raw = num / den if den else 0.0
            out[eng] = {"edge": raw, "edge_shrunk": raw * n / (n + SHRINK_K), "n": n}
        return out

    @staticmethod
    def _evaluate(samples: List[Dict[str, Any]], weights: Dict[str, float]) -> Dict[str, float]:
        """Expectancy of the shadows the consensus gate would still take with `weights`."""
        picked: List[float] = []
        for s in samples:
            c = consensus(_reports(s["evidence"]), weights, DIR_OF[s["side"]], DEFAULT_THRESHOLDS)
            if c["passed"]:
                picked.append(s["r_result"])
        n = len(picked)
        return {"n": n, "expectancy_r": (sum(picked) / n) if n else 0.0, "win_rate": (sum(1 for r in picked if r > 0) / n) if n else 0.0}

    # ---- learning pass --------------------------------------------------
    @staticmethod
    def _propose(train: List[Dict[str, Any]], active: Dict[str, float]) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
        """Bounded proposal from the edges measured on `train` (shrunk, step-capped, absolute bounds)."""
        base = AGENT_WEIGHTS
        edges = WeightLearner.edges(train)
        proposal = dict(active)
        for eng, e in edges.items():
            target = base[eng] * max(0.8, min(1.2, 1 + 0.5 * math.tanh(e["edge_shrunk"] / 0.5)))
            lo, hi = active[eng] * (1 - STEP_CAP), active[eng] * (1 + STEP_CAP)
            proposal[eng] = round(max(base[eng] * ABS_LOW, min(base[eng] * ABS_HIGH, max(lo, min(hi, target)))), 4)
        return proposal, edges

    def run(self) -> Dict[str, Any]:
        """Expanding-window rolling walk-forward: every fold proposes weights from its past only and is judged on the
        strictly later window. A proposal is stored (for MANUAL approval) only when it beats the active weights on the
        pooled out-of-sample folds AND in a majority of folds."""
        samples = sorted(self.shadows.resolved(), key=lambda s: s["ts"])
        report: Dict[str, Any] = {"samples": len(samples), "min_samples": self.cfg.ai_min_samples, "status": "insufficient_data",
                                  "method": "expanding_window_rolling_walk_forward"}
        if len(samples) < self.cfg.ai_min_samples:
            return report
        version, active = self.active()
        folds = make_folds(len(samples), min_train=max(30, int(len(samples) * 0.4)), n_splits=WF_SPLITS)
        if len(folds) < 2:
            report["status"] = "insufficient_walk_forward_folds"
            return report
        fold_rows: List[Dict[str, Any]] = []
        for k, f in enumerate(folds, 1):
            train, test = samples[:f.train_end], samples[f.test_start:f.test_end]
            if max(s["ts"] for s in train) >= min(s["ts"] for s in test):
                raise ValueError("walk-forward leakage detected")
            proposal_k, _ = self._propose(train, active)
            b, p = self._evaluate(test, active), self._evaluate(test, proposal_k)
            fold_rows.append({"fold": k, "train": len(train), "test": len(test), "train_end_ts": train[-1]["ts"], "test_start_ts": test[0]["ts"],
                              "baseline": b, "proposed": p, "gain_r": round(p["expectancy_r"] - b["expectancy_r"], 4),
                              "won": p["n"] > 0 and p["expectancy_r"] > b["expectancy_r"]})
        def pooled(key: str) -> Dict[str, float]:
            n = sum(r[key]["n"] for r in fold_rows)
            tot = sum(r[key]["expectancy_r"] * r[key]["n"] for r in fold_rows)
            wins_n = sum(r[key]["win_rate"] * r[key]["n"] for r in fold_rows)
            return {"n": n, "expectancy_r": tot / n if n else 0.0, "win_rate": wins_n / n if n else None}
        b, p = pooled("baseline"), pooled("proposed")
        wins = sum(1 for r in fold_rows if r["won"])
        eligible = (p["n"] >= max(10, int(0.4 * b["n"])) and p["expectancy_r"] >= b["expectancy_r"] + MIN_EXPECTANCY_GAIN
                    and wins > len(fold_rows) / 2)
        proposal, edges = self._propose(samples, active)          # deployable candidate: fit on all history AFTER validation
        metrics = {
            "folds": fold_rows, "fold_count": len(fold_rows), "folds_won": wins, "baseline": b, "proposed": p, "eligible": eligible,
            "top_agents": sorted(((k, round(v["edge_shrunk"], 3), int(v["n"])) for k, v in edges.items()), key=lambda x: -x[1])[:5],
            "bottom_agents": sorted(((k, round(v["edge_shrunk"], 3), int(v["n"])) for k, v in edges.items()), key=lambda x: x[1])[:5],
        }
        changed = {k: (round(active[k], 3), v) for k, v in proposal.items() if abs(v - active[k]) > 1e-9}
        report.update({"status": "proposed" if eligible else "no_improvement", "metrics": metrics, "changed": changed})
        if not changed:
            report["status"] = "no_change"
            return report
        if eligible:
            ver = self.db.add_weights(proposal, "proposed", metrics, "agents-1 rolling walk-forward validated")
            report["version"] = ver
            self.db.event("INFO", "ai_proposal", f"AI weight proposal v{ver}: out-of-sample {b['expectancy_r']:+.2f}R -> {p['expectancy_r']:+.2f}R, "
                                                 f"won {wins}/{len(fold_rows)} folds", {"folds": len(fold_rows)})
            report["status"] = "proposed_manual_review"         # never auto-promoted
        else:
            self.db.event("INFO", "ai_no_improvement", f"AI proposal did not beat the active weights out of sample ({wins}/{len(fold_rows)} folds)")
        return report

    def summary(self) -> Dict[str, Any]:
        res = self.shadows.resolved()
        n = len(res)
        pending = self.db.one("SELECT COUNT(*) AS c FROM shadows WHERE status='OPEN'")["c"]
        version, _ = self.active()
        trades = self.db.query("SELECT mode, r_multiple, pnl_usdt FROM trades WHERE status='CLOSED'")
        stats: Dict[str, Any] = {"active_version": version, "resolved_shadows": n, "open_shadows": pending,
                                 "pending_proposals": [{"version": p["version"], "metrics": {k: v for k, v in (p["metrics"] or {}).items() if k != "folds"}}
                                                       for p in self.proposals()],
                                 "performance": performance_report(res, trades)}
        if n:
            rs = [r["r_result"] for r in res]
            stats["shadow_expectancy_r"] = round(sum(rs) / n, 3)
            stats["shadow_win_rate"] = round(sum(1 for r in rs if r > 0) / n, 3)
            by_style: Dict[str, List[float]] = {}
            for r in res:
                by_style.setdefault(r["style"], []).append(r["r_result"])
            stats["by_style"] = {k: {"n": len(v), "expectancy_r": round(sum(v) / len(v), 3)} for k, v in by_style.items()}
        return stats
