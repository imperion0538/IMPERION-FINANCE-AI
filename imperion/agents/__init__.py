"""The agent team. Each agent reads one aspect of the market; the director (core/director.py) decides."""
from .base import Agent, AgentContext
from .context import DerivativesAgent, ExternalAgent, MarketContextAgent
from .flow import MicrostructureAgent, MomentumAgent, OrderFlowAgent
from .price import LiquidityAgent, StructureAgent, TrendAgent, TriggerAgent, ZoneAgent


def default_team():
    return [TrendAgent(), StructureAgent(), LiquidityAgent(), ZoneAgent(), TriggerAgent(), OrderFlowAgent(),
            MicrostructureAgent(), MomentumAgent(), MarketContextAgent(), DerivativesAgent(), ExternalAgent()]


AGENT_NAMES = tuple(a.name for a in default_team())

__all__ = ["Agent", "AgentContext", "default_team", "AGENT_NAMES"]
