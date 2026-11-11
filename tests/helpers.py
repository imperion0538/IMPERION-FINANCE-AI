import math
import random
from typing import List, Optional

from imperion.models import Candle


def make_candles(closes: List[float], *, step: int = 60_000, t0: int = 1_700_000_000_000, vol: float = 100.0,
                 buy_frac: float = 0.5, wick: float = 0.002, buy_fracs: Optional[List[float]] = None) -> List[Candle]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi, lo = max(o, c) * (1 + wick), min(o, c) * (1 - wick)
        bf = buy_fracs[i] if buy_fracs else buy_frac
        out.append(Candle(t0 + i * step, o, hi, lo, c, vol, t0 + (i + 1) * step - 1, vol * c, 100, vol * bf))
        prev = c
    return out


def trend(n: int, start: float = 100.0, drift: float = 0.0015, amp: float = 0.012, period: float = 9.0) -> List[float]:
    return [start * math.exp(drift * i) * (1 + amp * math.sin(i / period * 2 * math.pi)) for i in range(n)]


def random_walk(n: int, seed: int, start: float = 100.0, vol: float = 0.004) -> List[float]:
    r = random.Random(seed)
    x, out = start, []
    for _ in range(n):
        x *= math.exp(r.gauss(0, vol))
        out.append(x)
    return out


def zigzag(pivots: List[float], steps: int = 6, *, step_ms: int = 60_000, t0: int = 1_700_000_000_000, wick: float = 0.0005,
           vol: float = 100.0) -> List[Candle]:
    """Piecewise-linear closes between pivot prices (`steps` candles per leg); aggressive buying on up-candles."""
    closes = [pivots[0]]
    for a, b in zip(pivots, pivots[1:]):
        closes += [a + (b - a) * k / steps for k in range(1, steps + 1)]
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi, lo = max(o, c) * (1 + wick), min(o, c) * (1 - wick)
        bf = 0.65 if c > o else 0.35 if c < o else 0.5
        out.append(Candle(t0 + i * step_ms, o, hi, lo, c, vol, t0 + (i + 1) * step_ms - 1, vol * c, 100, vol * bf))
        prev = c
    return out


def bar(i: int, o: float, h: float, l: float, c: float, *, v: float = 100.0, bf: float = 0.5, step_ms: int = 60_000,
        t0: int = 1_700_000_000_000) -> Candle:
    return Candle(t0 + i * step_ms, o, h, l, c, v, t0 + (i + 1) * step_ms - 1, v * c, 10, v * bf)


def append(cs: List[Candle], o: float, h: float, l: float, c: float, **kw) -> List[Candle]:
    step = (cs[1].t - cs[0].t) if len(cs) > 1 else 60_000
    cs.append(bar(len(cs), o, h, l, c, step_ms=step, t0=cs[0].t, **kw))
    return cs


def crafted_analyze(fake, side: str = "LONG", playbook: str = "trend_pullback", grade: str = "A", quality: float = 82.0,
                    stop_pct: float = 0.025):
    """Replacement for Engine.director.analyze returning an approved plan (runtime tests must not depend on PA luck)."""
    from imperion.agents import AGENT_NAMES
    from imperion.analysis.priceaction.types import Playbook
    from imperion.models import DIR_OF, AgentReport, Analysis, TradeSetup
    from imperion.util import now_ms

    def analyze(symbol, style_name, hint=None, now=None, data_ok=True, health_quality=1.0):
        px = fake.price(symbol)
        sign = 1 if side == "LONG" else -1
        dist = px * stop_pct
        setup = TradeSetup(symbol, style_name, side, px, px - sign * dist, px + sign * 1.2 * dist, px + sign * 2.5 * dist, dist / 2, 2.5,
                           stop_pct * 100, ["crafted"], playbook, grade)
        d = DIR_OF[side]
        names = [n for n in AGENT_NAMES if n != "external"]
        reports = {n: AgentReport(n, d, 0.7, f"{n} agrees") for n in names}
        pb = Playbook(playbook, side, 0.8, ["crafted step"], setup.stop, setup.tp2)
        other = "SHORT" if side == "LONG" else "LONG"
        cons = {side: {"direction": d, "net": 0.6, "agree": names, "oppose": [], "neutral": [], "opposition": 0.0, "flow_confirmed": True,
                       "passed": True, "reasons": []},
                other: {"direction": "x", "net": -0.6, "agree": [], "oppose": names, "neutral": [], "opposition": 1.0, "flow_confirmed": False,
                        "passed": False, "reasons": ["WEAK_CONSENSUS"]}}
        return Analysis(symbol, style_name, now or now_ms(), price=px, direction=side if data_ok else "NO TRADE", approved=data_ok,
                        reasons=[f"{playbook} {side}", "crafted"] if data_ok else ["DATA_BLOCKED"], reports=reports,
                        regime={"regime": "TREND_UP" if side == "LONG" else "TREND_DOWN", "session": "LONDON", "profile": "TREND", "volatility": "NORMAL"},
                        playbooks=[pb], playbook=pb, consensus=cons, setup=setup, quality=quality, grade=grade, mtf={}, data_ok=data_ok)
    return analyze
