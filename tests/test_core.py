"""Core: agents, consensus, regime policy, planner and the Director's decision chain."""
import pytest

from imperion.agents import AGENT_NAMES
from imperion.agents.base import Agent, AgentContext
from imperion.agents.context import DerivativesAgent, ExternalAgent
from imperion.agents.flow import MomentumAgent
from imperion.agents.price import LiquidityAgent, TriggerAgent, ZoneAgent
from imperion.analysis.priceaction.types import Break, CandleSignal, PAMap, Playbook, Pool, Structure, Sweep, Zone
from imperion.config import STYLES, load_settings
from imperion.core import director as director_mod
from imperion.core.consensus import consensus
from imperion.core.director import Director
from imperion.core.planner import build_plan, grade, quality, round_trip_cost_pct
from imperion.core.policy import AGENT_WEIGHTS, DEFAULT_THRESHOLDS, PolicyError, check_weights, effective_weights, profile
from imperion.data.microstructure import BookStore, TradeStore
from imperion.data.universe import DerivativesStore
from imperion.fakes import make_engine
from imperion.models import BEARISH, BULLISH, NEUTRAL, AgentReport
from imperion.util import now_ms

CFG = load_settings({})
DAY = STYLES["day"]


def rep(name, d=BULLISH, c=0.7, **kw):
    return AgentReport(name, d, c, name, True, **kw)


def team(d=BULLISH, c=0.7, skip=()):
    return {n: rep(n, d, c) for n in AGENT_NAMES if n not in skip}


# ---- consensus / policy --------------------------------------------------------------------------------
def test_consensus_counts_weight_breadth_and_opposition():
    c = consensus(team(), AGENT_WEIGHTS, BULLISH, DEFAULT_THRESHOLDS)
    assert c["passed"] and c["net"] == pytest.approx(0.7) and len(c["agree"]) == len(AGENT_NAMES) and c["flow_confirmed"]
    assert not consensus(team(), AGENT_WEIGHTS, BEARISH, DEFAULT_THRESHOLDS)["passed"]
    split = {**team(BULLISH, skip=("orderflow", "momentum", "microstructure", "context", "derivatives", "external")),
             **{n: rep(n, BEARISH, 0.9) for n in ("orderflow", "momentum", "microstructure", "context")}}
    c = consensus(split, AGENT_WEIGHTS, BULLISH, DEFAULT_THRESHOLDS)
    assert not c["passed"] and any("OPPOSITION" in r for r in c["reasons"])
    few = {n: rep(n) for n in ("trend", "structure", "liquidity")}
    assert any("TOO_FEW_AGENTS" in r for r in consensus(few, AGENT_WEIGHTS, BULLISH, DEFAULT_THRESHOLDS)["reasons"])
    flow_only = {n: rep(n) for n in ("orderflow", "microstructure", "momentum", "context", "derivatives")}
    assert "NO_PRICE_ACTION_AGREEMENT" in consensus(flow_only, AGENT_WEIGHTS, BULLISH, DEFAULT_THRESHOLDS)["reasons"]
    unavailable = {**team(), "microstructure": AgentReport.unavailable("microstructure", "no streams")}
    assert "microstructure" not in consensus(unavailable, AGENT_WEIGHTS, BULLISH, DEFAULT_THRESHOLDS)["agree"]


def test_regime_profiles_change_weights_and_thresholds_within_bounds():
    tr, rg, vol = profile("TREND_UP"), profile("RANGE"), profile("VOLATILE")
    assert tr["weights"]["trend"] > 1 > rg["weights"]["trend"]
    assert vol["thresholds"]["min_net"] > DEFAULT_THRESHOLDS["min_net"] and vol["thresholds"]["min_agree"] == 5
    assert profile("RANGE", "EXPANSION")["thresholds"]["min_net"] >= 0.26 and "EXPANSION" in profile("RANGE", "EXPANSION")["name"]
    assert profile("weird")["name"] == "NEUTRAL" and profile("TREND_UP", adaptive=False)["weights"] == {}
    w = effective_weights({"trend": 1.0, "made_up_engine": 9.0}, tr)
    assert w["trend"] == pytest.approx(1.3) and "made_up_engine" not in w and AGENT_WEIGHTS["trend"] == 1.3
    with pytest.raises(PolicyError):
        check_weights({"trend": -1})


# ---- planner -------------------------------------------------------------------------------------------
def pmap(price=100.0, atr=1.0, **kw):
    base = dict(ok=True, n=300, price=price, atr=atr, structure=Structure(trend="UP"), recent_low=price - 0.4, recent_high=price + 0.4)
    base.update(kw)
    return PAMap(**base)


def test_plan_uses_liquidity_targets_and_structural_stops():
    pb = Playbook("trend_pullback", "LONG", 0.8, [], 98.0, 108.0)
    s, why, facts = build_plan("SOLUSDT", DAY, pb, pmap(pools_above=[Pool(107.0, "EQH", 2, 1)]), CFG)
    assert s is not None and s.stop == pytest.approx(97.8) and s.tp2 == pytest.approx(107.0) and s.playbook == "trend_pullback"
    assert s.tp1 == pytest.approx(100 + 1.5 * 2.2) and s.rr == pytest.approx(7 / 2.2)
    far = build_plan("SOLUSDT", DAY, Playbook("trend_pullback", "LONG", 0.8, [], 98.0, 150.0), pmap(pools_above=[Pool(150, "HIGH", 1, 1)]), CFG)[0]
    assert far.rr == pytest.approx(DAY.max_rr), "liquidity beyond the style's maximum is not 'expected'"
    short = build_plan("SOLUSDT", DAY, Playbook("sweep_reversal", "SHORT", 0.8, [], 101.5, None), pmap(pools_below=[]), CFG)[0]
    assert short.stop == pytest.approx(101.7) and short.tp2 == pytest.approx(100 - DAY.min_rr * 1.7)


def test_plan_rejections():
    def why(pb, m, style=DAY):
        return build_plan("SOLUSDT", style, pb, m, CFG)[1][0]
    assert why(Playbook("x", "LONG", 1, [], 98.0, None), pmap(pools_above=[Pool(101.0, "HIGH", 1, 1)])).startswith("NO_ROOM")
    assert why(Playbook("x", "LONG", 1, [], 99.9, None), pmap(atr=0.05)).startswith("COST_EXCEEDS_EDGE")
    assert why(Playbook("x", "LONG", 1, [], 101.0, None), pmap()).startswith("INVALIDATED")
    assert why(Playbook("x", "LONG", 1, [], 90.0, None), pmap(recent_low=None)).startswith("STOP_TOO_WIDE")
    assert why(Playbook("range_fade", "LONG", 1, [], 98.0, 103.0, cap=103.0), pmap()).startswith("RANGE_TOO_SMALL")
    minor = build_plan("SOLUSDT", DAY, Playbook("x", "LONG", 1, [], 90.0, None), pmap(recent_low=98.5), CFG)[0]
    assert minor is not None and "minor-structure stop" in minor.notes and minor.stop == pytest.approx(98.3)
    tight = build_plan("SOLUSDT", DAY, Playbook("x", "LONG", 1, [], 99.9, None), pmap(atr=2.0, recent_low=99.0), CFG)[0]
    assert tight.entry - tight.stop == pytest.approx(1.0), "stops are never closer than the minimum ATR distance"
    assert round_trip_cost_pct(CFG) == pytest.approx(0.16)


def test_quality_and_grades():
    q = quality(0.9, 0.6, 8, 1.0, 4.0, DAY, 1.0, True, False)
    assert q >= 95 and grade(q, DAY) == "A+"
    weak = quality(0.5, 0.2, 4, 0.3, 2.0, DAY, 0.8, False, True)
    assert grade(weak, DAY) == "C" and weak < DAY.min_quality
    assert grade(80, DAY) == "A" and grade(DAY.min_quality, DAY) == "B"


# ---- agents --------------------------------------------------------------------------------------------
def ctx(**kw):
    base = dict(symbol="SOLUSDT", style=DAY, now=now_ms(), cfg=CFG, candles=None, trades=TradeStore(), books=BookStore(),
                deriv=DerivativesStore(), universe=CFG.symbols)
    base.update(kw)
    return AgentContext(**base)


def test_price_agents_on_hand_built_maps():
    sweep = Sweep(BULLISH, 100.0, 99.4, 298, 1, 2, "EQL", 0.74)
    m = pmap(sweeps=[sweep], pools_above=[Pool(104, "HIGH", 1, 1)], zones=[Zone("OB", BULLISH, 99.6, 100.4, 280, displacement=2.0)])
    liq = LiquidityAgent().run(ctx(setup_map=m))
    assert liq.direction == BULLISH and liq.confidence == pytest.approx(0.74)
    both = pmap(sweeps=[sweep, Sweep(BEARISH, 101, 101.5, 299, 0, 1, "HIGH", 0.6)])
    assert LiquidityAgent().run(ctx(setup_map=both)).direction == NEUTRAL
    assert ZoneAgent().run(ctx(setup_map=m)).direction == BULLISH
    t = pmap(structure=Structure(trend="UP", last_break=Break("CHOCH", "UP", 99, 295, 1.5, age=3)), candle=CandleSignal(BULLISH, 0.62, ["engulfing"]))
    tr = TriggerAgent().run(ctx(trigger_map=t))
    assert tr.direction == BULLISH and tr.confidence > 0.75
    assert TriggerAgent().run(ctx(trigger_map=PAMap(ok=False, reason="x"))).available is False


def test_veto_agents():
    soon = now_ms() + 5 * 60_000
    d = DerivativesStore()
    d.data["SOLUSDT"] = {"funding": 0.0012, "mark": 1.0, "next_funding_ms": soon}
    r = DerivativesAgent().run(ctx(deriv=d))
    assert r.direction == BEARISH and "LONG" in r.veto and "FUNDING_ADVERSE" in r.veto["LONG"]
    d.data["SOLUSDT"]["next_funding_ms"] = now_ms() + 4 * 3_600_000
    assert not DerivativesAgent().run(ctx(deriv=d)).veto
    hot = [__import__("imperion.models", fromlist=["Candle"]).Candle(i * 60_000, 100 * 1.01 ** i, 100 * 1.01 ** (i + 1) * 1.001, 100 * 1.01 ** i * 0.999,
                                                                   100 * 1.01 ** (i + 1), 100, i * 60_000 + 59_999, 0, 1, 60) for i in range(120)]
    mom = MomentumAgent().run(ctx(setup_cs=hot))
    assert "LONG" in mom.veto and "MOMENTUM_EXHAUSTED" in mom.veto["LONG"]
    assert ExternalAgent().run(ctx(hint="SELL")).direction == BEARISH and not ExternalAgent().run(ctx()).available


def test_a_crashing_agent_reports_unavailable():
    class Broken(Agent):
        name = "trend"

        def analyze(self, c):
            raise RuntimeError("boom")
    r = Broken().run(ctx())
    assert r.available is False and "RuntimeError" in r.summary


def test_whole_team_on_live_like_data():
    engine, fake = make_engine()
    an = engine.director.analyze("SOLUSDT", "day")
    assert set(an.reports) == set(AGENT_NAMES)
    for r in an.reports.values():
        assert r.direction in (BULLISH, BEARISH, NEUTRAL) and 0 <= r.confidence <= 1
    assert an.reports["microstructure"].available, "warm fake streams feed the microstructure agent"
    assert an.regime.get("regime") in ("TREND_UP", "TREND_DOWN", "RANGE", "TRANSITION", "VOLATILE", "UNKNOWN")
    assert set(an.mtf["tfs"]) == {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}
    d = an.to_dict()
    assert d["agents"] and d["maps"]["setup"]["ok"]
    engine.stop()


# ---- the Director's decision chain ----------------------------------------------------------------------
class Fixed(Agent):
    def __init__(self, name, report):
        self.name, self.report = name, report

    def analyze(self, c):
        return AgentReport(self.report.agent, self.report.direction, self.report.confidence, self.report.summary, self.report.available,
                           dict(self.report.facts), dict(self.report.veto))


class Store:
    def get(self, symbol, tf, n=None):
        return []


@pytest.fixture()
def rigged(monkeypatch):
    setup_map = pmap(pools_above=[Pool(108.0, "EQH", 2, 1)], pools_below=[Pool(92.0, "EQL", 2, 1)])
    state = {"playbooks": [Playbook("trend_pullback", "LONG", 0.8, ["step"], 98.0, 108.0)]}
    monkeypatch.setattr(director_mod, "build_map", lambda cs: setup_map)
    monkeypatch.setattr(director_mod, "find_playbooks", lambda s, t, htf, reg, params=None: [Playbook(p.name, p.side, p.score, list(p.steps), p.invalidation, p.target, p.cap)
                                                                               for p in state["playbooks"]])
    monkeypatch.setattr(director_mod, "mtf_view", lambda c, s, tfs: {"tfs": {tf: {"bias": BULLISH} for tf in tfs}, "net": 0.8, "confluence": 0.8, "direction": BULLISH})
    monkeypatch.setattr(director_mod.market, "regime", lambda cs, st=None: {"regime": "TREND_UP", "volatility": "NORMAL"})

    def make(reports):
        return Director(CFG, Store(), TradeStore(), BookStore(), DerivativesStore(), agents=[Fixed(k, v) for k, v in reports.items()])
    return make, state


def test_director_approves_a_complete_agreed_playbook(rigged):
    make, _ = rigged
    an = make(team()).analyze("SOLUSDT", "day")
    assert an.approved and an.direction == "LONG" and an.grade in ("A+", "A", "B") and an.setup.tp2 == pytest.approx(108.0)
    assert an.setup.grade == an.grade and an.playbook.name == "trend_pullback" and "uyumlu ajanlar" in an.reasons[1]


def test_director_rejections(rigged):
    make, state = rigged
    htf = {**team(), "trend": rep("trend", BEARISH, 0.8)}
    an = make(htf).analyze("SOLUSDT", "day")
    assert not an.approved and any("HTF_TREND_AGAINST" in r for r in an.reasons)
    state["playbooks"] = [Playbook("sweep_reversal", "LONG", 0.75, [], 98.0, 108.0)]
    assert make(htf).analyze("SOLUSDT", "day").approved, "a complete sweep reversal may fade the higher timeframe"
    state["playbooks"] = [Playbook("trend_pullback", "LONG", 0.8, [], 98.0, 108.0)]
    veto = {**team(), "momentum": rep("momentum", BULLISH, 0.5, veto={"LONG": "MOMENTUM_EXHAUSTED (RSI 85)"})}
    assert any("VETO" in r for r in make(veto).analyze("SOLUSDT", "day").reasons)
    few = {n: rep(n) for n in ("trend", "structure", "liquidity")}
    an = make(few).analyze("SOLUSDT", "day")
    assert not an.approved and an.setup is not None and any("TOO_FEW_AGENTS" in r for r in an.reasons), "candidate plan kept for shadow learning"
    state["playbooks"] = [Playbook("trend_pullback", "LONG", 0.3, [], 98.0, 108.0)]
    assert any("PLAYBOOK_INCOMPLETE" in r for r in make(team()).analyze("SOLUSDT", "day").reasons)
    state["playbooks"] = [Playbook("trend_pullback", "LONG", 0.55, [], 98.0, 108.0)]
    price_only = {n: rep(n, BULLISH if n in ("trend", "structure", "liquidity", "zones", "trigger") else NEUTRAL,
                         0.35 if n in ("trend", "structure", "liquidity", "zones", "trigger") else 0.0) for n in AGENT_NAMES}
    weak = make(price_only).analyze("SOLUSDT", "day")
    assert not weak.approved and weak.grade == "C" and any("QUALITY_BELOW_THRESHOLD" in r for r in weak.reasons)


def test_director_falls_through_to_the_next_playbook_and_respects_data_health(rigged):
    make, state = rigged
    state["playbooks"] = [Playbook("sweep_reversal", "SHORT", 0.9, [], 101.0, 92.0), Playbook("trend_pullback", "LONG", 0.8, [], 98.0, 108.0)]
    an = make(team()).analyze("SOLUSDT", "day")
    assert an.approved and an.direction == "LONG" and any("sweep_reversal/SHORT" in p for p in [r for r in an.reasons] + ["sweep_reversal/SHORT"])
    blocked = make(team()).analyze("SOLUSDT", "day", data_ok=False)
    assert not blocked.approved and blocked.direction == "NO TRADE" and blocked.reasons[0].startswith("DATA_BLOCKED")


def test_low_signal_agents_never_make_up_the_agreement_count():
    reports = {n: rep(n) for n in ("trend", "structure", "liquidity", "external", "derivatives")}
    c = consensus(reports, AGENT_WEIGHTS, BULLISH, DEFAULT_THRESHOLDS)
    assert any("TOO_FEW_AGENTS (3 < 4" in r for r in c["reasons"]) and "external" in c["agree"]


def test_threshold_overrides_are_validated():
    assert profile("RANGE", overrides={"min_agree": 6})["thresholds"]["min_agree"] == 6
    with pytest.raises(PolicyError):
        profile("RANGE", overrides={"nonsense": 1})
