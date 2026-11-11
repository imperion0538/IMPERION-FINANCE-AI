"""Context agents: market leader + breadth, derivatives (funding) and external hints (TradingView)."""
from __future__ import annotations

from ..analysis.market import breadth
from ..analysis.mtf import TF_WEIGHT, cached_tf_bias
from ..models import BEARISH, BULLISH, NEUTRAL, AgentReport
from ..util import clamp
from .base import Agent, AgentContext

LEADER = "BTCUSDT"


class MarketContextAgent(Agent):
    name = "context"
    description = "Piyasa lideri (BTC) eğilimi ve evren genişliği"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        score, weight, parts, facts = 0.0, 0.0, [], {}
        if ctx.symbol != LEADER and LEADER in ctx.universe:
            num = den = 0.0
            for tf in ctx.style.htf_bias:
                b = cached_tf_bias(ctx.candles, LEADER, tf)
                if b["bias"] != "UNKNOWN":
                    w = TF_WEIGHT.get(tf, 1.0)
                    num += w * b["score"]
                    den += w
            if den:
                lead = num / den
                score += 0.6 * lead
                weight += 0.6
                facts["btc"] = round(lead, 3)
                parts.append(f"BTC {lead:+.2f}")
        b = breadth(ctx.candles, ctx.universe, "1h")
        if b["available"]:
            br = clamp((b["share"] - 0.5) * 2.5, -1, 1)
            score += 0.4 * br
            weight += 0.4
            facts["breadth"] = b
            parts.append(f"genişlik %{b['share'] * 100:.0f}")
        if not weight:
            return AgentReport.unavailable(self.name, "bağlam verisi yok")
        net = score / weight
        d = BULLISH if net > 0.2 else BEARISH if net < -0.2 else NEUTRAL
        return AgentReport(self.name, d, clamp(abs(net), 0, 0.8) if d != NEUTRAL else 0.0, ", ".join(parts), True, facts)


class DerivativesAgent(Agent):
    name = "derivatives"
    description = "Fonlama oranı: kalabalık pozisyonlara karşı uyarı, aleyhte fonlama ödemesinden önce veto"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        d = ctx.deriv.get(ctx.symbol)
        if not d:
            return AgentReport.unavailable(self.name, "fonlama verisi yok")
        rate = d["funding"] * 100
        veto = {}
        nxt = d.get("next_funding_ms") or 0
        minutes = (nxt - ctx.now) / 60_000 if nxt else None
        cfg = ctx.cfg
        if minutes is not None and 0 <= minutes <= cfg.funding_block_minutes and abs(rate) >= cfg.max_adverse_funding_pct > 0:
            payer = "LONG" if rate > 0 else "SHORT"
            veto[payer] = f"FUNDING_ADVERSE ({rate:+.3f}% {minutes:.0f} dk içinde, {payer} öder)"
        facts = {"funding_pct": round(rate, 4), "minutes_to_funding": round(minutes, 1) if minutes is not None else None}
        if abs(rate) < 0.05:
            return AgentReport(self.name, NEUTRAL, 0.0, f"fonlama {rate:+.3f}% (nötr)", True, facts, veto)
        direction = BEARISH if rate > 0 else BULLISH
        return AgentReport(self.name, direction, clamp(0.25 + (abs(rate) - 0.05) * 3, 0.25, 0.6),
                           f"fonlama {rate:+.3f}%: kalabalık {'long' if rate > 0 else 'short'}", True, facts, veto)


class ExternalAgent(Agent):
    name = "external"
    description = "TradingView uyarısı (yalnızca ipucu)"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        h = (ctx.hint or "").upper()
        if h in ("LONG", "BUY", "BULLISH"):
            return AgentReport(self.name, BULLISH, 0.6, "TradingView: long", True)
        if h in ("SHORT", "SELL", "BEARISH"):
            return AgentReport(self.name, BEARISH, 0.6, "TradingView: short", True)
        return AgentReport.unavailable(self.name, "uyarı yok")
