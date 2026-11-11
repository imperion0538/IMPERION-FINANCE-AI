"""Flow and momentum agents: candle order flow, live microstructure, momentum."""
from __future__ import annotations

from ..analysis.market import momentum
from ..analysis.orderflow import candle_flow, live_flow
from ..models import BEARISH, BULLISH, NEUTRAL, AgentReport
from ..util import clamp
from .base import Agent, AgentContext


def _directional(score: float, threshold: float = 0.15):
    if abs(score) < threshold:
        return NEUTRAL, 0.0
    return (BULLISH if score > 0 else BEARISH), clamp(0.3 + abs(score) * 0.6, 0.3, 0.9)


class OrderFlowAgent(Agent):
    name = "orderflow"
    description = "Tetik zaman diliminde agresif alım/satım: delta, CVD, uyumsuzluk, emilim"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        f = candle_flow(ctx.trigger_cs)
        if not f["available"]:
            return AgentReport.unavailable(self.name, "yeterli mum yok")
        d, conf = _directional(f["score"])
        parts = [f"delta {f['delta3']:+.2f}", f"CVD eğimi {f['cvd_slope']:+.2f}"]
        if f["divergence"]:
            parts.append("CVD uyumsuzluğu " + ("yükseliş" if f["divergence"] > 0 else "düşüş"))
        if f["absorption"]:
            parts.append("emilim " + ("alıcılar" if f["absorption"] > 0 else "satıcılar"))
        return AgentReport(self.name, d, conf, ", ".join(parts), True, f)


class MicrostructureAgent(Agent):
    name = "microstructure"
    description = "Canlı akış: emir defteri dengesi, balina işlemleri, footprint"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        f = live_flow(ctx.trades, ctx.books, ctx.symbol, ctx.now)
        if not f["available"]:
            return AgentReport.unavailable(self.name, "canlı akış verisi yok / ısınıyor")
        d, conf = _directional(f["score"])
        parts = []
        if "book_imbalance" in f:
            parts.append(f"defter {f['book_imbalance']:+.2f}")
        if "whale_net_15m" in f:
            parts.append(f"balina net {f['whale_net_15m']:+,.0f} USDT/15dk")
        return AgentReport(self.name, d, conf * min(1.0, f["coverage"] + 0.2), ", ".join(parts) or "canlı akış dengeli", True, f)


class MomentumAgent(Agent):
    name = "momentum"
    description = "RSI, MACD histogramı ve ADX/DI; aşırı uçlarda geç kalınmış girişleri veto eder"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        m = momentum(ctx.setup_cs)
        if not m["available"]:
            return AgentReport.unavailable(self.name, "yeterli mum yok")
        d, conf = _directional(m["score"], 0.2)
        veto = {}
        if m["exhausted"] == "UP":
            veto["LONG"] = f"MOMENTUM_EXHAUSTED (RSI {m['rsi']:.0f})"
        elif m["exhausted"] == "DOWN":
            veto["SHORT"] = f"MOMENTUM_EXHAUSTED (RSI {m['rsi']:.0f})"
        return AgentReport(self.name, d, conf, f"RSI {m['rsi']:.0f}, ADX {m['adx'] if m['adx'] is not None else '-'}", True, m, veto)
