"""Agent contract. Every agent reads one aspect of the market and returns an AgentReport; none of them can trade."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..analysis.priceaction.types import PAMap
from ..config import Settings, StyleConfig
from ..models import AgentReport, Candle

log = logging.getLogger("imperion.agents")


@dataclass
class AgentContext:
    symbol: str
    style: StyleConfig
    now: int
    cfg: Settings
    candles: Any                      # CandleStore-like: get(symbol, tf, n=None)
    trades: Any                       # TradeStore
    books: Any                        # BookStore
    deriv: Any                        # DerivativesStore
    universe: Sequence[str]
    setup_cs: List[Candle] = field(default_factory=list)
    trigger_cs: List[Candle] = field(default_factory=list)
    setup_map: Optional[PAMap] = None
    trigger_map: Optional[PAMap] = None
    regime: Dict[str, Any] = field(default_factory=dict)
    mtf: Dict[str, Any] = field(default_factory=dict)
    hint: Optional[str] = None


class Agent:
    name = "agent"
    description = ""

    def analyze(self, ctx: AgentContext) -> AgentReport:      # pragma: no cover - interface
        raise NotImplementedError

    def run(self, ctx: AgentContext) -> AgentReport:
        """Never raises: a failing agent reports itself unavailable, the director carries on without it."""
        try:
            rep = self.analyze(ctx)
            rep.agent = self.name
            rep.confidence = max(0.0, min(1.0, float(rep.confidence)))
            return rep
        except Exception as exc:
            log.exception("agent %s failed for %s", self.name, ctx.symbol)
            return AgentReport.unavailable(self.name, f"agent error: {type(exc).__name__}")
