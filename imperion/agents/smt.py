"""SMT Agent: correlated-pair divergence as an independent agent report.

Uses confirmed-swing SMT between the analyzed symbol and its predefined pair.
Reports direction, confidence and can softly influence consensus (no hard veto by default).
"""
from __future__ import annotations

from typing import Optional

from ..analysis.priceaction.smt import smt_divergence
from ..models import BEARISH, BULLISH, NEUTRAL, AgentReport
from .base import Agent, AgentContext

# Same pairs as Director (keep in sync or import)
SMT_PAIRS = {
    "BTCUSDT": "ETHUSDT",
    "ETHUSDT": "BTCUSDT",
    "SOLUSDT": "ETHUSDT",
    "BNBUSDT": "BTCUSDT",
    "XRPUSDT": "BTCUSDT",
    "ADAUSDT": "ETHUSDT",
    "AVAXUSDT": "ETHUSDT",
    "LINKUSDT": "ETHUSDT",
    "DOTUSDT": "ETHUSDT",
    "LTCUSDT": "BTCUSDT",
    "DOGEUSDT": "BTCUSDT",
    "SUIUSDT": "SOLUSDT",
    "NEARUSDT": "ETHUSDT",
    "APTUSDT": "ETHUSDT",
    "BCHUSDT": "BTCUSDT",
    "TRXUSDT": "ETHUSDT",
}


class SMTAgent(Agent):
    name = "smt"
    description = "Korelasyonlu çiftte SMT divergence (HH/LL uyumsuzluğu)"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        pair = SMT_PAIRS.get(ctx.symbol)
        if not pair:
            return AgentReport.unavailable(self.name, "no correlated pair defined")

        setup_cs = ctx.setup_cs
        if not setup_cs or len(setup_cs) < 25:
            return AgentReport.unavailable(self.name, "insufficient primary candles")

        comp_cs = ctx.candles.get(pair, ctx.style.setup_tf) if ctx.candles else None
        if not comp_cs or len(comp_cs) < 25:
            return AgentReport.unavailable(self.name, f"no candles for pair {pair}")

        result = smt_divergence(setup_cs, comp_cs)
        if not result.get("available"):
            return AgentReport.unavailable(self.name, result.get("reason", "smt unavailable"))

        direction = result.get("direction", "NONE")
        signals = result.get("signals") or []
        if direction in ("NONE", "CONFLICT") or not signals:
            return AgentReport(
                agent=self.name,
                direction=NEUTRAL,
                confidence=0.25,
                summary=f"SMT {direction} vs {pair}",
                facts={"pair": pair, "smt": result},
            )

        # Map to BULLISH / BEARISH
        d = BULLISH if direction == "BULLISH" else BEARISH
        # Confidence based on number and clarity of signals
        conf = min(0.85, 0.55 + 0.12 * len(signals))
        summary = f"SMT {direction} vs {pair} ({len(signals)} signal)"
        if signals:
            summary += f" [{signals[0].get('type', '')}]"

        return AgentReport(
            agent=self.name,
            direction=d,
            confidence=conf,
            summary=summary,
            facts={"pair": pair, "smt": result, "signals": len(signals)},
        )
