"""Price-action agents: higher-timeframe trend, setup structure, liquidity, zones and the trigger timeframe."""
from __future__ import annotations

from ..analysis.mtf import TF_WEIGHT, cached_tf_bias
from ..analysis.priceaction.playbooks import confirmation
from ..analysis.priceaction.types import PAMap
from ..analysis.priceaction.zones import describe, zone_quality, zones_at_price
from ..models import BEARISH, BULLISH, NEUTRAL, AgentReport
from ..util import clamp
from .base import Agent, AgentContext

TR = {BULLISH: "yükseliş", BEARISH: "düşüş", NEUTRAL: "nötr", "UNKNOWN": "?"}


class TrendAgent(Agent):
    name = "trend"
    description = "Üst zaman dilimlerinde yapı (BOS/CHoCH) + EMA dizilimi"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        num = den = 0.0
        facts, parts = {}, []
        for tf in ctx.style.htf_bias:
            b = cached_tf_bias(ctx.candles, ctx.symbol, tf)
            facts[tf] = b
            if b["bias"] == "UNKNOWN":
                continue
            w = TF_WEIGHT.get(tf, 1.0)
            num += w * b["score"]
            den += w
            parts.append(f"{tf} {TR[b['bias']]}")
        if not den:
            return AgentReport.unavailable(self.name, "üst zaman dilimi verisi yok")
        net = num / den
        d = BULLISH if net > 0.15 else BEARISH if net < -0.15 else NEUTRAL
        return AgentReport(self.name, d, abs(net) if d != NEUTRAL else 0.0, " · ".join(parts), True, {"net": round(net, 3), "tfs": facts})


class StructureAgent(Agent):
    name = "structure"
    description = "Kurulum zaman diliminde piyasa yapısı ve korunan seviye"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        m = ctx.setup_map
        if m is None or not m.ok:
            return AgentReport.unavailable(self.name, m.reason if m else "harita yok")
        st, lb = m.structure, m.structure.last_break
        facts = {"trend": st.trend, "swing_bias": st.swing_bias, "protected": st.protected,
                 "range_position": round(m.range.position, 3) if m.range else None}
        if st.trend not in ("UP", "DOWN") or lb is None:
            return AgentReport(self.name, NEUTRAL, 0.0, f"yapı {st.trend}", True, facts)
        d = BULLISH if st.trend == "UP" else BEARISH
        conf = 0.45 + 0.20 * clamp(lb.displacement / 2.0, 0, 1)
        if (st.swing_bias == "HH_HL" and d == BULLISH) or (st.swing_bias == "LH_LL" and d == BEARISH):
            conf += 0.10
        if lb.kind == "CHOCH":
            conf -= 0.05
        if lb.age > 40:
            conf -= 0.10
        facts.update(last_break=lb.kind, age=lb.age, displacement=round(lb.displacement, 2))
        return AgentReport(self.name, d, clamp(conf, 0.2, 0.85), f"{st.trend} — son {lb.kind} {lb.age} mum önce ({lb.displacement:.1f} ATR)", True, facts)


class LiquidityAgent(Agent):
    name = "liquidity"
    description = "Likidite havuzları ve süpürmeler (stop avı)"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        m = ctx.setup_map
        if m is None or not m.ok:
            return AgentReport.unavailable(self.name, "harita yok")
        sweeps = [(s, "kurulum") for s in m.sweeps if s.age <= 6]
        if ctx.trigger_map is not None and ctx.trigger_map.ok:
            sweeps += [(s, "tetik") for s in ctx.trigger_map.sweeps if s.age <= 10]
        a = m.atr
        facts = {"pools_above": [(round((p.price - m.price) / a, 2), p.kind) for p in m.pools_above[:3]],
                 "pools_below": [(round((m.price - p.price) / a, 2), p.kind) for p in m.pools_below[:3]]}
        if not sweeps:
            return AgentReport(self.name, NEUTRAL, 0.0, "yakın zamanda süpürme yok", True, facts)
        best = {}
        for s, where in sweeps:
            if s.direction not in best or s.quality > best[s.direction][0].quality:
                best[s.direction] = (s, where)
        if len(best) > 1:
            return AgentReport(self.name, NEUTRAL, 0.0, "iki yönde de süpürme (çatışma)", True, facts)
        (s, where), = best.values()
        facts.update(sweep={"kind": s.kind, "level": s.level, "age": s.age, "tf": where})
        side = "satış tarafı (altta)" if s.direction == BULLISH else "alış tarafı (üstte)"
        return AgentReport(self.name, s.direction, s.quality, f"{where} TF {side} {s.kind} likiditesi süpürüldü", True, facts)


class ZoneAgent(Agent):
    name = "zones"
    description = "Fiyatın içinde bulunduğu OB / FVG / breaker / IFVG bölgeleri"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        m = ctx.setup_map
        if m is None or not m.ok:
            return AgentReport.unavailable(self.name, "harita yok")
        sup = zones_at_price(m.zones, m.price, m.atr, BULLISH)
        res = zones_at_price(m.zones, m.price, m.atr, BEARISH)
        facts = {"support": [describe(z) for z in sup[:3]], "resistance": [describe(z) for z in res[:3]]}
        if sup and not res:
            q = max(zone_quality(z) for z in sup)
            return AgentReport(self.name, BULLISH, 0.35 + 0.45 * q, "destek bölgesinde: " + describe(max(sup, key=zone_quality)), True, facts)
        if res and not sup:
            q = max(zone_quality(z) for z in res)
            return AgentReport(self.name, BEARISH, 0.35 + 0.45 * q, "direnç bölgesinde: " + describe(max(res, key=zone_quality)), True, facts)
        return AgentReport(self.name, NEUTRAL, 0.0, "destek ve direnç bölgesi iç içe" if sup else "fiyat bir bölgede değil", True, facts)


class TriggerAgent(Agent):
    name = "trigger"
    description = "Tetik zaman diliminde dönüş/devam onayı (mum formasyonu, yapı kırılımı, yer değiştirme)"

    def analyze(self, ctx: AgentContext) -> AgentReport:
        t = ctx.trigger_map
        if t is None or not t.ok:
            return AgentReport.unavailable(self.name, t.reason if t else "harita yok")
        empty = PAMap(ok=False)
        bull, bsteps = confirmation(empty, t, BULLISH)
        bear, ssteps = confirmation(empty, t, BEARISH)
        if max(bull, bear) < 0.3 or abs(bull - bear) < 0.15:
            return AgentReport(self.name, NEUTRAL, 0.0, "tetik zaman diliminde net onay yok", True, {"bull": bull, "bear": bear})
        d, conf, steps = (BULLISH, bull, bsteps) if bull > bear else (BEARISH, bear, ssteps)
        return AgentReport(self.name, d, conf, "; ".join(steps), True, {"bull": round(bull, 3), "bear": round(bear, 3)})
