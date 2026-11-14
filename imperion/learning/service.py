"""Learning service: the AI second opinion at decision time and the periodic learning cycle.

The AI layer can only BLOCK a setup (proven-negative context, low win probability from a VALIDATED model). It never
creates or enlarges a trade, and agent-weight proposals always need explicit human approval.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from ..config import Settings
from ..db import Database
from ..models import Analysis
from . import model as win_model
from .autonomous import ExperienceMemory, AutonomousMind, research_summary
from .context import ContextStats
from .controlled import validate_learning
from .learner import WeightLearner
from .shadow import ShadowTracker

log = logging.getLogger("imperion.learning")
CONTEXT_REFRESH_S = 600


class LearningService:
    def __init__(self, cfg: Settings, db: Database, shadows: ShadowTracker):
        self.cfg, self.db, self.shadows = cfg, db, shadows
        self.learner = WeightLearner(db, cfg, shadows)
        self.model: Optional[win_model.WinModel] = win_model.WinModel.from_dict(db.kv_get("ai.model"))
        self._ctx: Tuple[float, Optional[ContextStats]] = (0.0, None)
        self._autonomous_memory: Tuple[float, Optional[ExperienceMemory]] = (0.0, None)

    @property
    def min_context_samples(self) -> int:
        return max(30, self.cfg.ai_min_samples // 2)

    @staticmethod
    def _approved(samples):
        """The context filter judges what it gates: setups the Director approved (rejected candidates are weaker)."""
        return [s for s in samples if (s.get("meta") or {}).get("approved", True)]

    def context_stats(self) -> ContextStats:
        ts, stats = self._ctx
        if stats is None or time.time() - ts >= CONTEXT_REFRESH_S:
            stats = ContextStats(self._approved(self.shadows.resolved()))
            self._ctx = (time.time(), stats)
        return stats

    def experience_memory(self) -> ExperienceMemory:
        """Cache the history reader: decisions must never re-scan full DB."""
        stamp, memory = self._autonomous_memory
        if memory is None or time.time() - stamp >= CONTEXT_REFRESH_S:
            memory = ExperienceMemory(self.shadows.resolved())
            self._autonomous_memory = (time.time(), memory)
        return memory

    def opinion(self, an: Analysis) -> Dict[str, Any]:
        out: Dict[str, Any] = {"blocked": False, "reasons": [], "p_win": None, "model_validated": False, "contexts": []}
        if an.direction not in ("LONG", "SHORT"):
            return out
        pb = an.playbook.name if an.playbook is not None else None
        try:
            if self.cfg.ai_context_filter:
                v = self.context_stats().verdict(an.style, an.regime.get("regime"), an.symbol, an.direction, an.regime.get("session"),
                                                 an.quality, self.min_context_samples, pb)
                out["contexts"] = v["matches"]
                out["reasons"] += v["reasons"]
            if self.model is not None:
                p = self.model.predict(an.snapshot(), an.direction, pb)
                out["p_win"], out["model_validated"] = round(p, 3), self.model.validated
                if self.cfg.ai_veto and self.model.validated and p < self.cfg.ai_min_win_prob:
                    out["reasons"].append(f"AI_LOW_WIN_PROBABILITY (p={p:.2f} < {self.cfg.ai_min_win_prob:.2f})")
        except Exception as exc:          # the AI layer is optional: a failure never breaks the decision path
            log.warning("AI opinion failed: %s: %s", type(exc).__name__, exc)
        # Additional historical context is descriptive ONLY. No extra veto,
        # no new trades, no learned weight or risk changes.
        try:
            if an.direction in ("LONG", "SHORT"):
                memory = self.experience_memory()
                ctx = {"regime":an.regime.get("regime"), "session":an.regime.get("session"),
                       "grade":an.grade, "playbook":an.playbook.name if an.playbook else None}
                out["autonomous_experience"] = memory.verdict(ctx,an.symbol)
        except Exception as exc:
            log.debug("autonomous memory unavailable: %s", type(exc).__name__)
            out["autonomous_experience"] = {"samples":0,"reliable":False}
        out["blocked"] = bool(out["reasons"])
        return out

    def cycle(self) -> Dict[str, Any]:
        """Weight proposal (manual approval), win-probability model (walk-forward), context statistics."""
        rep = self.learner.run()
        rep["controlled_learning_review"] = validate_learning((rep.get("metrics") or {}).get("proposed") or {}, min_samples=self.min_context_samples)
        rep["promotion_policy"] = "MANUAL_APPROVAL_ONLY"
        samples = self.shadows.resolved()
        model, model_rep = win_model.train(samples, self.cfg.ai_min_samples)
        if model is not None:
            self.model = model
            self.db.kv_set("ai.model", model.to_dict())
            self.db.event("INFO", "ai_model", f"win-probability model retrained: {model_rep['status']} {model_rep.get('metrics')}")
        rep["win_model"] = model_rep
        stats = ContextStats(self._approved(samples))
        self._ctx = (time.time(), stats)
        # Never auto-approve learned weights or change the Guardian policy.
        rep["autonomous"] = research_summary(samples)
        self.db.kv_set("autonomous.last_report", rep["autonomous"])
        self._autonomous_memory = (time.time(), ExperienceMemory(samples))
        rep["contexts"] = {"worst": stats.worst(self.min_context_samples), "best": stats.best(self.min_context_samples),
                           "filter": "on" if self.cfg.ai_context_filter else "off", "veto": "on" if self.cfg.ai_veto else "off"}
        self.db.kv_set("ai.last_report", rep)
        return rep

    def summary(self) -> Dict[str, Any]:
        out = self.learner.summary()
        out["win_model"] = self.model.to_dict() if self.model else None
        out.update(context_filter=self.cfg.ai_context_filter, veto=self.cfg.ai_veto, min_win_prob=self.cfg.ai_min_win_prob)
        out["autonomous"] = self.db.kv_get("autonomous.last_report") or research_summary([])
        return out
