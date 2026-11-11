"""The Director ("müdür"): runs the agent team, matches playbooks and decides LONG / SHORT / NO TRADE.

Decision chain (every step is logged as a reason, nothing is hidden in a single score):
  1. price-action maps of the setup and trigger timeframes, 8-timeframe view, market regime
  2. every agent reports (trend, structure, liquidity, zones, trigger, order flow, microstructure, momentum,
     market context, derivatives, external hint)
  3. playbooks: complete setup models (sweep reversal, trend pullback, breakout retest, range fade)
  4. for each playbook, best first:
       completeness  >= threshold (regime dependent)
       higher-timeframe trend not strongly against it (sweep reversals that are complete enough are exempt)
       no agent veto (momentum exhaustion, imminent adverse funding, ...)
       an executable plan exists (stop, liquidity targets, room, costs)
       the team agrees: weighted net, number of agreeing agents, opposition, price-action agreement
       quality grade B or better
  5. the first playbook that passes everything is the decision.
The Director never sees the account and never places orders; risk, AI vetoes and execution belong to the runtime.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional, Sequence, Tuple
import inspect

from ..agents import AgentContext, default_team
from ..analysis import market
from ..analysis.mtf import alignment, mtf_view
from ..analysis.priceaction import build_map, summary
from ..analysis.priceaction.smt import smt_divergence
from ..analysis.priceaction.confluence import adaptive_thresholds
from ..analysis.priceaction.market_models import detect_mmbm_candidate
from ..analysis.priceaction.playbooks import find_playbooks
from ..config import STYLES, TIMEFRAMES, Settings
from ..models import DIR_OF, NEUTRAL, Analysis
from ..util import now_ms
from .consensus import consensus
from .planner import build_plan, grade, quality
from .policy import effective_weights, profile

WeightsProvider = Callable[[], Tuple[int, Dict[str, float]]]


class Director:
    def __init__(self, cfg: Settings, candles, trades, books, deriv, weights_provider: Optional[WeightsProvider] = None,
                 agents: Optional[Sequence] = None, threshold_overrides: Optional[Dict[str, float]] = None):
        self.cfg, self.candles, self.trades, self.books, self.deriv = cfg, candles, trades, books, deriv
        self.threshold_overrides = dict(threshold_overrides or {})
        self.params = None          # optional ParameterSet injected for research / optimization
        self.agents = list(agents) if agents is not None else default_team()
        self.weights_provider = weights_provider or (lambda: (0, {}))

    def analyze(self, symbol: str, style_name: str, hint: Optional[str] = None, now: Optional[int] = None,
                data_ok: bool = True, health_quality: float = 1.0) -> Analysis:
        style = STYLES[style_name]
        now = now if now is not None else now_ms()
        setup_cs = self.candles.get(symbol, style.setup_tf)
        trig_cs = self.candles.get(symbol, style.trigger_tf)
        smap, tmap = build_map(setup_cs), build_map(trig_cs)
        mtf = mtf_view(self.candles, symbol, TIMEFRAMES)
        reg = market.regime(setup_cs, smap.structure.trend if smap.ok else None)
        reg["session"] = market.session(now)
        ctx = AgentContext(symbol, style, now, self.cfg, self.candles, self.trades, self.books, self.deriv, tuple(self.cfg.symbols),
                           setup_cs, trig_cs, smap, tmap, reg, mtf, hint)
        reports = {a.name: a.run(ctx) for a in self.agents}
        live = self.trades.last_price(symbol) if self.trades is not None else None
        price = live or (trig_cs[-1].c if trig_cs else None) or (smap.price if smap.ok else None)
        an = Analysis(symbol, style_name, now, price=price, reports=reports, regime=reg, mtf=mtf,
                      maps={"setup": summary(smap), "trigger": summary(tmap)}, data_ok=data_ok)
        wver, learned = self.weights_provider()
        prof = profile(reg.get("regime"), reg.get("volatility"), self.cfg.regime_adaptive, self.threshold_overrides)
        weights, th = effective_weights(learned, prof), prof["thresholds"]
        an.weights_version = wver
        an.regime["profile"] = prof["name"]
        an.consensus = {side: consensus(reports, weights, DIR_OF[side], th) for side in ("LONG", "SHORT")}
        if not smap.ok:
            an.reasons = [f"SETUP_MAP_UNAVAILABLE ({smap.reason})"]
            return an
        trend = reports.get("trend")
        htf = {"direction": trend.direction, "strength": trend.confidence} if trend is not None and trend.available else None
        # Inter-market SMT: use only explicitly paired, same-timeframe series.
        pairs = {"BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT", "SOLUSDT": "ETHUSDT",
                 "BNBUSDT": "ETHUSDT", "XRPUSDT": "BTCUSDT", "ADAUSDT": "BTCUSDT"}
        comparison_symbol = pairs.get(symbol.upper())
        smt = None
        if comparison_symbol and comparison_symbol in self.cfg.symbols:
            comparison_cs = self.candles.get(comparison_symbol, style.setup_tf)
            if comparison_cs:
                smt = smt_divergence(setup_cs, comparison_cs)
                smt["comparison_symbol"] = comparison_symbol
                smt["primary_symbol"] = symbol.upper()
        flow = None
        if self.trades is not None and hasattr(self.trades, "flow_profile"):
            try:
                flow = self.trades.flow_profile(symbol, minutes=5, now=now)
            except Exception:
                flow = {"available": False, "reason": "flow_profile_error"}
        market_model = detect_mmbm_candidate(setup_cs, smap.atr) if smap.ok else {"available": False}
        thresholds = adaptive_thresholds(reg.get("volatility") or reg.get("regime") or "NORMAL")
        intelligence = {"smt": smt, "flow": flow, "market_model": market_model, "thresholds": thresholds}
        an.regime["market_intelligence"] = {
            "smt": smt,
            "structure": {"internal": smap.structure.internal_trend, "external": smap.structure.external_trend},
            "displacement_quality": smap.displacement_quality,
            "displacement_facts": smap.displacement_facts, "flow_divergence": smap.flow_divergence,
            "flow": flow, "market_model": market_model, "adaptive_thresholds": thresholds,
        }
        # Keep the Director seam compatible with test doubles / downstream adapters
        # that still expose the V15 five-argument playbook callable.
        if "intelligence" in inspect.signature(find_playbooks).parameters:
            an.playbooks = find_playbooks(smap, tmap, htf, reg.get("regime") or "UNKNOWN", self.params,
                                          intelligence=intelligence)
        else:
            an.playbooks = find_playbooks(smap, tmap, htf, reg.get("regime") or "UNKNOWN", self.params)
        if not an.playbooks:
            an.reasons = ["NO_PLAYBOOK (no complete price-action setup)"]
            return an
        rejected = []
        for pb in an.playbooks:
            tag = f"{pb.name}/{pb.side}"
            d = DIR_OF[pb.side]
            if pb.score < th["min_playbook"]:
                rejected.append(f"{tag}: PLAYBOOK_INCOMPLETE ({pb.score:.2f} < {th['min_playbook']:.2f})")
                continue
            if (htf and htf["direction"] not in (d, NEUTRAL) and htf["strength"] >= th["htf_veto"]
                    and not (pb.name == "sweep_reversal" and pb.score >= th["htf_veto_exempt"])):
                rejected.append(f"{tag}: HTF_TREND_AGAINST (strength {htf['strength']:.2f})")
                continue
            vetoes = [r.veto[pb.side] for r in reports.values() if r.available and pb.side in r.veto]
            if vetoes:
                rejected.append(f"{tag}: VETO " + "; ".join(vetoes))
                continue
            setup, why, facts = build_plan(symbol, style, pb, smap, self.cfg, price)
            if setup is None:
                rejected.append(f"{tag}: " + "; ".join(why))
                continue
            c = an.consensus[pb.side]
            q = quality(pb.score, c["net"], len(c["agree"]), alignment(mtf, d), setup.rr, style, health_quality,
                        c["flow_confirmed"], facts.get("obstacle_before_tp2", False))
            g = grade(q, style)
            setup.grade = g
            if an.setup is None:                      # best executable candidate (shadow-tracked even if rejected)
                an.setup, an.playbook, an.quality, an.grade = setup, pb, q, g
            if not c["passed"]:
                rejected.append(f"{tag}: " + "; ".join(c["reasons"]))
                continue
            if g == "C":
                rejected.append(f"{tag}: QUALITY_BELOW_THRESHOLD ({q:.0f} < {style.min_quality:.0f})")
                continue
            an.setup, an.playbook, an.quality, an.grade = setup, pb, q, g
            if not data_ok:
                an.reasons = ["DATA_BLOCKED (market data not healthy)", f"candidate: {tag} grade {g}"]
                return an
            an.direction, an.approved = pb.side, True
            an.reasons = [f"{pb.name} {pb.side} — derece {g} (kalite {q:.0f})", "uyumlu ajanlar: " + ", ".join(c["agree"])] + pb.steps
            return an
        an.reasons = (["DATA_BLOCKED (market data not healthy)"] if not data_ok else []) + rejected[:6]
        return an
