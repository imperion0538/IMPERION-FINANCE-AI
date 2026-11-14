
"""IMPERION FINANCE AI V17 - Advanced Decision Director.

Coordinates market analysis, agents, playbooks, consensus and
trade planning.

Decision pipeline:
    PERCEIVE -> UNDERSTAND -> DECIDE -> PROTECT

The Director does not place orders or access account balances.
An approved analysis is still subject to runtime risk guards,
portfolio limits and execution checks.

Public compatibility:
    Director(...)
    Director.analyze(...)
    director.params
    director.threshold_overrides
    director.weights_provider
"""
from __future__ import annotations

import inspect
import math
from typing import Callable, Dict, Optional, Sequence, Tuple

from ..agents import AgentContext, default_team
from ..agents.autonomous import AutonomousIntelligence
from ..learning.autonomous import AutonomousMind
from ..analysis import market
from ..analysis.mtf import alignment, mtf_view
from ..analysis.priceaction import build_map, summary
from ..analysis.priceaction.confluence import adaptive_thresholds
from ..analysis.priceaction.market_models import (
    detect_mmbm_candidate,
)
from ..analysis.priceaction.playbooks import find_playbooks
from ..analysis.priceaction.smt import smt_divergence
from ..config import STYLES, TIMEFRAMES, Settings
from ..models import DIR_OF, NEUTRAL, Analysis
from ..util import now_ms
from .consensus import consensus
from .planner import build_plan, grade, quality
from .policy import effective_weights, profile


WeightsProvider = Callable[
    [], Tuple[int, Dict[str, float]]
]


SMT_PAIRS = {
    "BTCUSDT": "ETHUSDT",
    "ETHUSDT": "BTCUSDT",
    "SOLUSDT": "ETHUSDT",
    "BNBUSDT": "ETHUSDT",
    "XRPUSDT": "BTCUSDT",
    "ADAUSDT": "BTCUSDT",
}


def _finite(value) -> bool:
    """Accept finite numeric values, excluding booleans."""
    if isinstance(value, bool):
        return False

    if not isinstance(value, (int, float)):
        return False

    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _positive(value) -> bool:
    return _finite(value) and value > 0


def _health(value) -> float:
    """Normalize market-data health to a safe 0..1 score."""
    if not _finite(value):
        return 0.0

    return max(0.0, min(1.0, float(value)))


def _usable_price(value):
    """Return a valid market price or None."""
    return value if _positive(value) else None


class Director:
    """Orchestrates analysis without performing execution."""

    def __init__(
        self,
        cfg: Settings,
        candles,
        trades,
        books,
        deriv,
        weights_provider: Optional[WeightsProvider] = None,
        agents: Optional[Sequence] = None,
        threshold_overrides: Optional[Dict[str, float]] = None,
    ):
        self.cfg = cfg
        self.candles = candles
        self.trades = trades
        self.books = books
        self.deriv = deriv

        self.threshold_overrides = dict(
            threshold_overrides or {}
        )

        # Research/optimization ParameterSet injection point.
        self.params = None

        self.agents = (
            list(agents)
            if agents is not None
            else default_team()
        )

        self.autonomous_intelligence = AutonomousIntelligence()
        self.autonomous_mind = AutonomousMind()

        self.weights_provider = (
            weights_provider
            if weights_provider is not None
            else lambda: (0, {})
        )

    def _market_intelligence(
        self,
        symbol,
        style,
        now,
        setup_cs,
        smap,
        reg,
    ):
        """Collect optional market intelligence.

        Optional feed failures are recorded as unavailable;
        they never become positive evidence.
        """
        smt = None

        comparison_symbol = SMT_PAIRS.get(
            symbol.upper()
        )

        if (
            comparison_symbol
            and comparison_symbol in self.cfg.symbols
        ):
            try:
                comparison_cs = self.candles.get(
                    comparison_symbol,
                    style.setup_tf,
                )

                if comparison_cs:
                    smt = smt_divergence(
                        setup_cs,
                        comparison_cs,
                    )

                    if isinstance(smt, dict):
                        smt["comparison_symbol"] = (
                            comparison_symbol
                        )
                        smt["primary_symbol"] = (
                            symbol.upper()
                        )
                    else:
                        smt = None

            except Exception:
                smt = {
                    "available": False,
                    "reason": "smt_data_error",
                }

        flow = None

        if (
            self.trades is not None
            and hasattr(self.trades, "flow_profile")
        ):
            try:
                flow = self.trades.flow_profile(
                    symbol,
                    minutes=5,
                    now=now,
                )

            except Exception:
                flow = {
                    "available": False,
                    "reason": "flow_profile_error",
                }

        try:
            market_model = detect_mmbm_candidate(
                setup_cs,
                smap.atr,
            )

        except Exception:
            market_model = {
                "available": False,
                "reason": "market_model_error",
            }

        thresholds = adaptive_thresholds(
            reg.get("volatility")
            or reg.get("regime")
            or "NORMAL"
        )

        intelligence = {
            "smt": smt,
            "flow": flow,
            "market_model": market_model,
            "thresholds": thresholds,
        }

        details = {
            "smt": smt,
            "structure": {
                "internal": (
                    smap.structure.internal_trend
                ),
                "external": (
                    smap.structure.external_trend
                ),
            },
            "displacement_quality": (
                smap.displacement_quality
            ),
            "displacement_facts": (
                smap.displacement_facts
            ),
            "flow_divergence": (
                smap.flow_divergence
            ),
            "flow": flow,
            "market_model": market_model,
            "adaptive_thresholds": thresholds,
        }

        return intelligence, details

    def _find_playbooks(
        self,
        smap,
        tmap,
        htf,
        regime_name,
        intelligence,
    ):
        """Preserve compatibility with older playbook adapters."""
        args = (
            smap,
            tmap,
            htf,
            regime_name,
            self.params,
        )

        try:
            parameters = inspect.signature(
                find_playbooks
            ).parameters

        except (TypeError, ValueError):
            parameters = {}

        if "intelligence" in parameters:
            return find_playbooks(
                *args,
                intelligence=intelligence,
            )

        return find_playbooks(*args)

    def analyze(
        self,
        symbol: str,
        style_name: str,
        hint: Optional[str] = None,
        now: Optional[int] = None,
        data_ok: bool = True,
        health_quality: float = 1.0,
    ) -> Analysis:
        """Return an approved trade or a NO TRADE analysis."""
        style = STYLES[style_name]
        now = now if now is not None else now_ms()

        setup_cs = self.candles.get(
            symbol,
            style.setup_tf,
        )

        trig_cs = self.candles.get(
            symbol,
            style.trigger_tf,
        )

        smap = build_map(setup_cs)
        tmap = build_map(trig_cs)

        mtf = mtf_view(
            self.candles,
            symbol,
            TIMEFRAMES,
        )

        reg = market.regime(
            setup_cs,
            smap.structure.trend if smap.ok else None,
        )

        reg["session"] = market.session(now)

        ctx = AgentContext(
            symbol,
            style,
            now,
            self.cfg,
            self.candles,
            self.trades,
            self.books,
            self.deriv,
            tuple(self.cfg.symbols),
            setup_cs,
            trig_cs,
            smap,
            tmap,
            reg,
            mtf,
            hint,
        )

        reports = {
            agent.name: agent.run(ctx)
            for agent in self.agents
        }

        live = None

        if self.trades is not None:
            try:
                live = _usable_price(
                    self.trades.last_price(symbol)
                )
            except Exception:
                live = None

        trigger_price = None

        if trig_cs:
            trigger_price = _usable_price(
                trig_cs[-1].c
            )

        map_price = (
            _usable_price(smap.price)
            if smap.ok
            else None
        )

        price = (
            live
            if live is not None
            else trigger_price
            if trigger_price is not None
            else map_price
        )

        an = Analysis(
            symbol,
            style_name,
            now,
            price=price,
            reports=reports,
            regime=reg,
            mtf=mtf,
            maps={
                "setup": summary(smap),
                "trigger": summary(tmap),
            },
            data_ok=data_ok,
        )

        # AUTONOMOUS PRO 1.5 RL supplementary team: evidence-only and independent
        # of legacy eleven-agent consensus and all execution/risk gates.
        # Errors are explicitly unavailable, never positive evidence.
        try:
            autonomous = self.autonomous_intelligence.analyze(ctx)
            autonomous["thought"] = self.autonomous_mind.think(autonomous)
            an.regime["autonomous"] = autonomous
        except Exception as exc:
            an.regime["autonomous"] = {"mode": "shadow_only", "available": 0,
                                        "reason": f"agent adapter: {type(exc).__name__}"}

        # Fail-closed default:
        # A rejected candidate is never an approved trade.
        an.approved = False
        an.direction = "NO TRADE"

        wver, learned = self.weights_provider()

        prof = profile(
            reg.get("regime"),
            reg.get("volatility"),
            self.cfg.regime_adaptive,
            self.threshold_overrides,
        )

        weights = effective_weights(
            learned,
            prof,
        )

        thresholds = prof["thresholds"]

        an.weights_version = wver
        an.regime["profile"] = prof["name"]

        an.consensus = {
            side: consensus(
                reports,
                weights,
                DIR_OF[side],
                thresholds,
            )
            for side in ("LONG", "SHORT")
        }

        if not smap.ok:
            an.reasons = [
                "SETUP_MAP_UNAVAILABLE "
                f"({smap.reason})"
            ]
            return an

        if price is None:
            an.reasons = [
                "NO_VALID_MARKET_PRICE"
            ]
            return an

        trend = reports.get("trend")
        htf = None

        if (
            trend is not None
            and trend.available
            and _finite(trend.confidence)
        ):
            htf = {
                "direction": trend.direction,
                "strength": max(
                    0.0,
                    min(1.0, trend.confidence),
                ),
            }

        intelligence, details = (
            self._market_intelligence(
                symbol,
                style,
                now,
                setup_cs,
                smap,
                reg,
            )
        )

        an.regime["market_intelligence"] = details

        an.playbooks = self._find_playbooks(
            smap,
            tmap,
            htf,
            reg.get("regime") or "UNKNOWN",
            intelligence,
        )

        if not an.playbooks:
            an.reasons = [
                "NO_PLAYBOOK "
                "(no complete price-action setup)"
            ]
            return an

        rejected = []

        for pb in an.playbooks:
            tag = f"{pb.name}/{pb.side}"

            if pb.side not in ("LONG", "SHORT"):
                rejected.append(
                    f"{tag}: INVALID_PLAYBOOK_SIDE"
                )
                continue

            if (
                not _finite(pb.score)
                or not 0 <= pb.score <= 1
            ):
                rejected.append(
                    f"{tag}: INVALID_PLAYBOOK_SCORE"
                )
                continue

            direction = DIR_OF[pb.side]

            minimum_score = thresholds["min_playbook"]

            if pb.score < minimum_score:
                rejected.append(
                    f"{tag}: PLAYBOOK_INCOMPLETE "
                    f"({pb.score:.2f} < "
                    f"{minimum_score:.2f})"
                )
                continue

            htf_against = (
                htf is not None
                and htf["direction"] not in (
                    direction,
                    NEUTRAL,
                )
                and htf["strength"]
                >= thresholds["htf_veto"]
            )

            reversal_exempt = (
                pb.name == "sweep_reversal"
                and pb.score
                >= thresholds["htf_veto_exempt"]
            )

            if htf_against and not reversal_exempt:
                rejected.append(
                    f"{tag}: HTF_TREND_AGAINST "
                    f"(strength "
                    f"{htf['strength']:.2f})"
                )
                continue

            vetoes = [
                report.veto[pb.side]
                for report in reports.values()
                if report.available
                and pb.side in report.veto
            ]

            if vetoes:
                rejected.append(
                    f"{tag}: VETO "
                    + "; ".join(vetoes)
                )
                continue

            setup, why, facts = build_plan(
                symbol,
                style,
                pb,
                smap,
                self.cfg,
                price,
            )

            if setup is None:
                rejected.append(
                    f"{tag}: "
                    + "; ".join(why)
                )
                continue

            candidate_consensus = (
                an.consensus[pb.side]
            )

            candidate_quality = quality(
                pb.score,
                candidate_consensus["net"],
                len(candidate_consensus["agree"]),
                alignment(mtf, direction),
                setup.rr,
                style,
                _health(health_quality),
                candidate_consensus[
                    "flow_confirmed"
                ],
                facts.get(
                    "obstacle_before_tp2",
                    False,
                ),
            )

            candidate_grade = grade(
                candidate_quality,
                style,
            )

            setup.grade = candidate_grade

            # Shadow candidate for analysis only.
            # Never treat it as execution approval.
            if an.setup is None:
                an.setup = setup
                an.playbook = pb
                an.quality = candidate_quality
                an.grade = candidate_grade

            if not candidate_consensus["passed"]:
                rejected.append(
                    f"{tag}: "
                    + "; ".join(
                        candidate_consensus["reasons"]
                    )
                )
                continue

            if candidate_grade not in ("A+", "A"):
                rejected.append(
                    f"{tag}: QUALITY_A_PLUS_OR_A_REQUIRED "
                    f"(grade={candidate_grade}, score={candidate_quality:.1f}, minimum=77)"
                )
                continue

            if not data_ok:
                an.reasons = [
                    "DATA_BLOCKED "
                    "(market data not healthy)",
                    f"candidate: {tag} "
                    f"grade {candidate_grade}",
                ]
                return an

            if _health(health_quality) <= 0:
                an.reasons = [
                    "HEALTH_BLOCKED "
                    "(invalid or zero market health)",
                    f"candidate: {tag}",
                ]
                return an

            # Director-level approval only.
            # Runtime risk and execution guards
            # remain mandatory.
            an.setup = setup
            an.playbook = pb
            an.quality = candidate_quality
            an.grade = candidate_grade
            an.direction = pb.side
            an.approved = True

            an.reasons = [
                f"{pb.name} {pb.side} — "
                f"derece {candidate_grade} "
                f"(kalite {candidate_quality:.0f})",
                "uyumlu ajanlar: "
                + ", ".join(
                    candidate_consensus["agree"]
                ),
                *pb.steps,
            ]

            return an

        if not data_ok:
            rejected.insert(
                0,
                "DATA_BLOCKED "
                "(market data not healthy)",
            )

        an.reasons = (
            rejected[:6]
            if rejected
            else ["NO_TRADE_NO_APPROVED_CANDIDATE"]
        )

        return an


# DIRECTOR_PY_END
