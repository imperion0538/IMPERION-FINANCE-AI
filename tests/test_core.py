
"""IMPERION FINANCE AI V17 - Core integration tests.

Tests:
- Agent consensus and adaptive policy
- Safe trade planning and liquidity targets
- Structural invalidation and stop protection
- Agent behaviour and vetoes
- Director approval and NO TRADE decisions

The tests must not weaken production risk controls.
"""

import pytest

from imperion.agents import AGENT_NAMES
from imperion.agents.base import Agent, AgentContext
from imperion.agents.context import (
    DerivativesAgent,
    ExternalAgent,
)
from imperion.agents.flow import MomentumAgent
from imperion.agents.price import (
    LiquidityAgent,
    TriggerAgent,
    ZoneAgent,
)
from imperion.analysis.priceaction.types import (
    Break,
    CandleSignal,
    PAMap,
    Playbook,
    Pool,
    Structure,
    Sweep,
    Zone,
)
from imperion.config import STYLES, load_settings
from imperion.core import director as director_mod
from imperion.core.consensus import consensus
from imperion.core.director import Director
from imperion.core.planner import (
    build_plan,
    grade,
    quality,
    round_trip_cost_pct,
)
from imperion.core.policy import (
    AGENT_WEIGHTS,
    DEFAULT_THRESHOLDS,
    PolicyError,
    check_weights,
    effective_weights,
    profile,
)
from imperion.data.microstructure import (
    BookStore,
    TradeStore,
)
from imperion.data.universe import DerivativesStore
from imperion.fakes import make_engine
from imperion.models import (
    BEARISH,
    BULLISH,
    NEUTRAL,
    AgentReport,
)
from imperion.util import now_ms


CFG = load_settings({})
DAY = STYLES["day"]


def rep(name, d=BULLISH, c=0.7, **kw):
    return AgentReport(
        name, d, c, name, True, **kw
    )


def team(d=BULLISH, c=0.7, skip=()):
    return {
        name: rep(name, d, c)
        for name in AGENT_NAMES
        if name not in skip
    }


# --------------------------------------------------
# CONSENSUS AND POLICY
# --------------------------------------------------

def test_consensus_counts_weight_breadth_and_opposition():
    c = consensus(
        team(),
        AGENT_WEIGHTS,
        BULLISH,
        DEFAULT_THRESHOLDS,
    )

    assert c["passed"]
    assert c["net"] == pytest.approx(0.7)
    assert len(c["agree"]) == len(AGENT_NAMES)
    assert c["flow_confirmed"]

    assert not consensus(
        team(),
        AGENT_WEIGHTS,
        BEARISH,
        DEFAULT_THRESHOLDS,
    )["passed"]

    opposing = (
        "orderflow",
        "momentum",
        "microstructure",
        "context",
    )

    split = {
        **team(
            BULLISH,
            skip=(
                *opposing,
                "derivatives",
                "external",
            ),
        ),
        **{
            name: rep(name, BEARISH, 0.9)
            for name in opposing
        },
    }

    c = consensus(
        split,
        AGENT_WEIGHTS,
        BULLISH,
        DEFAULT_THRESHOLDS,
    )

    assert not c["passed"]
    assert any(
        "OPPOSITION" in reason
        for reason in c["reasons"]
    )

    few = {
        name: rep(name)
        for name in (
            "trend",
            "structure",
            "liquidity",
        )
    }

    c = consensus(
        few,
        AGENT_WEIGHTS,
        BULLISH,
        DEFAULT_THRESHOLDS,
    )

    assert any(
        "TOO_FEW_AGENTS" in reason
        for reason in c["reasons"]
    )

    flow_only = {
        name: rep(name)
        for name in (
            "orderflow",
            "microstructure",
            "momentum",
            "context",
            "derivatives",
        )
    }

    c = consensus(
        flow_only,
        AGENT_WEIGHTS,
        BULLISH,
        DEFAULT_THRESHOLDS,
    )

    assert "NO_PRICE_ACTION_AGREEMENT" in c["reasons"]

    unavailable = {
        **team(),
        "microstructure": AgentReport.unavailable(
            "microstructure",
            "no streams",
        ),
    }

    c = consensus(
        unavailable,
        AGENT_WEIGHTS,
        BULLISH,
        DEFAULT_THRESHOLDS,
    )

    assert "microstructure" not in c["agree"]


def test_regime_profiles_change_weights_and_thresholds_within_bounds():
    tr = profile("TREND_UP")
    rg = profile("RANGE")
    vol = profile("VOLATILE")

    assert tr["weights"]["trend"] > 1
    assert rg["weights"]["trend"] < 1

    assert (
        vol["thresholds"]["min_net"]
        > DEFAULT_THRESHOLDS["min_net"]
    )
    assert vol["thresholds"]["min_agree"] == 5

    expansion = profile(
        "RANGE",
        "EXPANSION",
    )

    assert expansion["thresholds"]["min_net"] >= 0.26
    assert "EXPANSION" in expansion["name"]

    assert profile("weird")["name"] == "NEUTRAL"
    assert profile(
        "TREND_UP",
        adaptive=False,
    )["weights"] == {}

    weights = effective_weights(
        {
            "trend": 1.0,
            "made_up_engine": 9.0,
        },
        tr,
    )

    assert weights["trend"] == pytest.approx(1.3)
    assert "made_up_engine" not in weights
    assert AGENT_WEIGHTS["trend"] == 1.3

    with pytest.raises(PolicyError):
        check_weights({"trend": -1})


# --------------------------------------------------
# PLANNER
# --------------------------------------------------

def pmap(price=100.0, atr=1.0, **kw):
    base = dict(
        ok=True,
        n=300,
        price=price,
        atr=atr,
        structure=Structure(trend="UP"),
        recent_low=price - 0.4,
        recent_high=price + 0.4,
    )
    base.update(kw)
    return PAMap(**base)


def test_plan_uses_liquidity_targets_and_structural_stops():
    pb = Playbook(
        "trend_pullback",
        "LONG",
        0.8,
        [],
        98.0,
        108.0,
    )

    setup, reasons, facts = build_plan(
        "SOLUSDT",
        DAY,
        pb,
        pmap(
            pools_above=[
                Pool(107.0, "EQH", 2, 1)
            ]
        ),
        CFG,
    )

    assert setup is not None, reasons
    assert setup.stop == pytest.approx(97.8)
    assert setup.tp2 == pytest.approx(107.0)
    assert setup.playbook == "trend_pullback"
    assert setup.rr == pytest.approx(7 / 2.2)

    assert setup.entry < setup.tp1 < setup.tp2
    assert setup.stop < pb.invalidation
    assert facts["validated"] is True

    # A target outside the maximum R/R is not
    # silently fabricated or pulled closer.
    far, reasons, facts = build_plan(
        "SOLUSDT",
        DAY,
        Playbook(
            "trend_pullback",
            "LONG",
            0.8,
            [],
            98.0,
            150.0,
        ),
        pmap(
            pools_above=[
                Pool(150.0, "HIGH", 1, 1)
            ]
        ),
        CFG,
    )

    assert far is None
    assert reasons[0] == "PLAN_NO_ELIGIBLE_TARGET"

    # No real downside liquidity target:
    # never invent a SHORT TP2.
    short, reasons, facts = build_plan(
        "SOLUSDT",
        DAY,
        Playbook(
            "sweep_reversal",
            "SHORT",
            0.8,
            [],
            101.5,
            None,
        ),
        pmap(pools_below=[]),
        CFG,
    )

    assert short is None
    assert reasons[0] == "PLAN_NO_ELIGIBLE_TARGET"


def test_plan_rejections():
    def plan(pb, market_map, style=DAY):
        return build_plan(
            "SOLUSDT",
            style,
            pb,
            market_map,
            CFG,
        )

    # Liquidity obstacle before the minimum
    # acceptable room must block the trade.
    setup, reasons, _ = plan(
        Playbook(
            "x", "LONG", 1, [], 98.0, None
        ),
        pmap(
            pools_above=[
                Pool(101.0, "HIGH", 1, 1)
            ]
        ),
    )

    assert setup is None
    assert (
        reasons[0]
        == "PLAN_INSUFFICIENT_LIQUIDITY_ROOM"
    )

    # Excessive trading costs must block
    # a trade with an insufficient edge.
    setup, reasons, _ = plan(
        Playbook(
            "x", "LONG", 1, [], 99.9, None
        ),
        pmap(
            atr=0.05,
            pools_above=[
                Pool(101.0, "HIGH", 1, 1)
            ],
        ),
    )

    assert setup is None
    assert reasons[0] == "PLAN_COST_EXCEEDS_EDGE"

    # Invalidated trade geometry must be rejected.
    setup, reasons, _ = plan(
        Playbook(
            "x", "LONG", 1, [], 101.0, None
        ),
        pmap(),
    )

    assert setup is None
    assert reasons[0] == "PLAN_ALREADY_INVALIDATED"

    # Stop beyond the style's maximum distance
    # must be rejected rather than tightened
    # inside the playbook invalidation.
    setup, reasons, _ = plan(
        Playbook(
            "x", "LONG", 1, [], 90.0, None
        ),
        pmap(recent_low=None),
    )

    assert setup is None
    assert reasons[0] == "STOP_TOO_WIDE"

    # A capped range must not bypass
    # the minimum acceptable reward.
    setup, reasons, _ = plan(
        Playbook(
            "range_fade",
            "LONG",
            1,
            [],
            98.0,
            103.0,
            cap=103.0,
        ),
        pmap(),
    )

    assert setup is None
    assert reasons

    # A nearby minor structure does not justify
    # moving the stop inside the playbook's
    # authoritative invalidation.
    setup, reasons, _ = plan(
        Playbook(
            "x", "LONG", 1, [], 90.0, None
        ),
        pmap(recent_low=98.5),
    )

    assert setup is None
    assert reasons[0] == "STOP_TOO_WIDE"

    # A minimum ATR distance may widen a stop,
    # but cannot violate structural invalidation.
    setup, reasons, facts = plan(
        Playbook(
            "x", "LONG", 1, [], 99.9, None
        ),
        pmap(
            atr=2.0,
            recent_low=99.0,
            pools_above=[
                Pool(104.0, "HIGH", 1, 1)
            ],
        ),
    )

    assert setup is not None, reasons
    assert setup.entry - setup.stop >= 1.0
    assert setup.stop < 99.9
    assert facts["validated"] is True

    assert round_trip_cost_pct(CFG) == pytest.approx(
        0.16
    )


def test_quality_and_grades():
    q = quality(
        0.9,
        0.6,
        8,
        1.0,
        4.0,
        DAY,
        1.0,
        True,
        False,
    )

    assert q >= 95
    assert grade(q, DAY) == "A+"

    weak = quality(
        0.5,
        0.2,
        4,
        0.3,
        2.0,
        DAY,
        0.8,
        False,
        True,
    )

    assert grade(weak, DAY) == "C"
    assert weak < DAY.min_quality

    assert grade(80, DAY) == "A"
    assert grade(DAY.min_quality, DAY) == "B"


# --------------------------------------------------
# AGENTS
# --------------------------------------------------

def ctx(**kw):
    base = dict(
        symbol="SOLUSDT",
        style=DAY,
        now=now_ms(),
        cfg=CFG,
        candles=None,
        trades=TradeStore(),
        books=BookStore(),
        deriv=DerivativesStore(),
        universe=CFG.symbols,
    )
    base.update(kw)
    return AgentContext(**base)


def test_price_agents_on_hand_built_maps():
    sweep = Sweep(
        BULLISH,
        100.0,
        99.4,
        298,
        1,
        2,
        "EQL",
        0.74,
    )

    market_map = pmap(
        sweeps=[sweep],
        pools_above=[
            Pool(104, "HIGH", 1, 1)
        ],
        zones=[
            Zone(
                "OB",
                BULLISH,
                99.6,
                100.4,
                280,
                displacement=2.0,
            )
        ],
    )

    liquidity = LiquidityAgent().run(
        ctx(setup_map=market_map)
    )

    assert liquidity.direction == BULLISH
    assert liquidity.confidence == pytest.approx(
        0.74
    )

    both = pmap(
        sweeps=[
            sweep,
            Sweep(
                BEARISH,
                101,
                101.5,
                299,
                0,
                1,
                "HIGH",
                0.6,
            ),
        ]
    )

    assert LiquidityAgent().run(
        ctx(setup_map=both)
    ).direction == NEUTRAL

    assert ZoneAgent().run(
        ctx(setup_map=market_map)
    ).direction == BULLISH

    trigger_map = pmap(
        structure=Structure(
            trend="UP",
            last_break=Break(
                "CHOCH",
                "UP",
                99,
                295,
                1.5,
                age=3,
            ),
        ),
        candle=CandleSignal(
            BULLISH,
            0.62,
            ["engulfing"],
        ),
    )

    trigger = TriggerAgent().run(
        ctx(trigger_map=trigger_map)
    )

    assert trigger.direction == BULLISH
    assert trigger.confidence > 0.75

    unavailable = TriggerAgent().run(
        ctx(
            trigger_map=PAMap(
                ok=False,
                reason="x",
            )
        )
    )

    assert unavailable.available is False


def test_veto_agents():
    soon = now_ms() + 5 * 60_000

    derivatives = DerivativesStore()
    derivatives.data["SOLUSDT"] = {
        "funding": 0.0012,
        "mark": 1.0,
        "next_funding_ms": soon,
    }

    report = DerivativesAgent().run(
        ctx(deriv=derivatives)
    )

    assert report.direction == BEARISH
    assert "LONG" in report.veto
    assert "FUNDING_ADVERSE" in report.veto["LONG"]

    derivatives.data["SOLUSDT"][
        "next_funding_ms"
    ] = now_ms() + 4 * 3_600_000

    assert not DerivativesAgent().run(
        ctx(deriv=derivatives)
    ).veto

    Candle = __import__(
        "imperion.models",
        fromlist=["Candle"],
    ).Candle

    hot = [
        Candle(
            i * 60_000,
            100 * 1.01 ** i,
            100 * 1.01 ** (i + 1) * 1.001,
            100 * 1.01 ** i * 0.999,
            100 * 1.01 ** (i + 1),
            100,
            i * 60_000 + 59_999,
            0,
            1,
            60,
        )
        for i in range(120)
    ]

    momentum = MomentumAgent().run(
        ctx(setup_cs=hot)
    )

    assert "LONG" in momentum.veto
    assert (
        "MOMENTUM_EXHAUSTED"
        in momentum.veto["LONG"]
    )

    assert ExternalAgent().run(
        ctx(hint="SELL")
    ).direction == BEARISH

    assert not ExternalAgent().run(
        ctx()
    ).available


def test_a_crashing_agent_reports_unavailable():
    class Broken(Agent):
        name = "trend"

        def analyze(self, context):
            raise RuntimeError("boom")

    report = Broken().run(ctx())

    assert report.available is False
    assert "RuntimeError" in report.summary


def test_whole_team_on_live_like_data():
    engine, fake = make_engine()

    try:
        analysis = engine.director.analyze(
            "SOLUSDT",
            "day",
        )

        assert set(analysis.reports) == set(
            AGENT_NAMES
        )

        for report in analysis.reports.values():
            assert report.direction in (
                BULLISH,
                BEARISH,
                NEUTRAL,
            )
            assert 0 <= report.confidence <= 1

        assert analysis.reports[
            "microstructure"
        ].available

        assert analysis.regime.get("regime") in (
            "TREND_UP",
            "TREND_DOWN",
            "RANGE",
            "TRANSITION",
            "VOLATILE",
            "UNKNOWN",
        )

        assert set(
            analysis.mtf["tfs"]
        ) == {
            "1m",
            "5m",
            "15m",
            "30m",
            "1h",
            "4h",
            "1d",
            "1w",
        }

        result = analysis.to_dict()

        assert result["agents"]
        assert result["maps"]["setup"]["ok"]

    finally:
        engine.stop()


# --------------------------------------------------
# DIRECTOR
# --------------------------------------------------

class Fixed(Agent):
    def __init__(self, name, report):
        self.name = name
        self.report = report

    def analyze(self, context):
        return AgentReport(
            self.report.agent,
            self.report.direction,
            self.report.confidence,
            self.report.summary,
            self.report.available,
            dict(self.report.facts),
            dict(self.report.veto),
        )


class Store:
    def get(self, symbol, tf, n=None):
        return []


@pytest.fixture()
def rigged(monkeypatch):
    setup_map = pmap(
        pools_above=[
            Pool(108.0, "EQH", 2, 1)
        ],
        pools_below=[
            Pool(92.0, "EQL", 2, 1)
        ],
    )

    state = {
        "playbooks": [
            Playbook(
                "trend_pullback",
                "LONG",
                0.8,
                ["step"],
                98.0,
                108.0,
            )
        ]
    }

    monkeypatch.setattr(
        director_mod,
        "build_map",
        lambda cs: setup_map,
    )

    def fake_playbooks(
        setup,
        trigger,
        htf,
        regime,
        params=None,
    ):
        return [
            Playbook(
                item.name,
                item.side,
                item.score,
                list(item.steps),
                item.invalidation,
                item.target,
                item.cap,
            )
            for item in state["playbooks"]
        ]

    monkeypatch.setattr(
        director_mod,
        "find_playbooks",
        fake_playbooks,
    )

    monkeypatch.setattr(
        director_mod,
        "mtf_view",
        lambda c, s, tfs: {
            "tfs": {
                tf: {"bias": BULLISH}
                for tf in tfs
            },
            "net": 0.8,
            "confluence": 0.8,
            "direction": BULLISH,
        },
    )

    monkeypatch.setattr(
        director_mod.market,
        "regime",
        lambda cs, st=None: {
            "regime": "TREND_UP",
            "volatility": "NORMAL",
        },
    )

    def make(reports):
        return Director(
            CFG,
            Store(),
            TradeStore(),
            BookStore(),
            DerivativesStore(),
            agents=[
                Fixed(name, report)
                for name, report in reports.items()
            ],
        )

    return make, state


def test_director_approves_a_complete_agreed_playbook(
    rigged,
):
    make, _ = rigged

    analysis = make(team()).analyze(
        "SOLUSDT",
        "day",
    )

    assert analysis.approved
    assert analysis.direction == "LONG"
    assert analysis.grade in ("A+", "A", "B")
    assert analysis.setup.tp2 == pytest.approx(
        108.0
    )
    assert analysis.setup.grade == analysis.grade
    assert analysis.playbook.name == "trend_pullback"
    assert "uyumlu ajanlar" in analysis.reasons[1]


def test_director_rejections(rigged):
    make, state = rigged

    htf = {
        **team(),
        "trend": rep(
            "trend",
            BEARISH,
            0.8,
        ),
    }

    analysis = make(htf).analyze(
        "SOLUSDT",
        "day",
    )

    assert not analysis.approved
    assert analysis.direction == "NO TRADE"
    assert any(
        "HTF_TREND_AGAINST" in reason
        for reason in analysis.reasons
    )

    state["playbooks"] = [
        Playbook(
            "sweep_reversal",
            "LONG",
            0.75,
            [],
            98.0,
            108.0,
        )
    ]

    assert make(htf).analyze(
        "SOLUSDT",
        "day",
    ).approved

    state["playbooks"] = [
        Playbook(
            "trend_pullback",
            "LONG",
            0.8,
            [],
            98.0,
            108.0,
        )
    ]

    veto = {
        **team(),
        "momentum": rep(
            "momentum",
            BULLISH,
            0.5,
            veto={
                "LONG": (
                    "MOMENTUM_EXHAUSTED (RSI 85)"
                )
            },
        ),
    }

    assert any(
        "VETO" in reason
        for reason in make(veto).analyze(
            "SOLUSDT",
            "day",
        ).reasons
    )

    few = {
        name: rep(name)
        for name in (
            "trend",
            "structure",
            "liquidity",
        )
    }

    analysis = make(few).analyze(
        "SOLUSDT",
        "day",
    )

    assert not analysis.approved
    assert analysis.direction == "NO TRADE"
    assert analysis.setup is not None
    assert any(
        "TOO_FEW_AGENTS" in reason
        for reason in analysis.reasons
    )

    state["playbooks"] = [
        Playbook(
            "trend_pullback",
            "LONG",
            0.3,
            [],
            98.0,
            108.0,
        )
    ]

    assert any(
        "PLAYBOOK_INCOMPLETE" in reason
        for reason in make(team()).analyze(
            "SOLUSDT",
            "day",
        ).reasons
    )

    state["playbooks"] = [
        Playbook(
            "trend_pullback",
            "LONG",
            0.55,
            [],
            98.0,
            108.0,
        )
    ]

    price_agents = (
        "trend",
        "structure",
        "liquidity",
        "zones",
        "trigger",
    )

    price_only = {
        name: rep(
            name,
            (
                BULLISH
                if name in price_agents
                else NEUTRAL
            ),
            (
                0.35
                if name in price_agents
                else 0.0
            ),
        )
        for name in AGENT_NAMES
    }

    weak = make(price_only).analyze(
        "SOLUSDT",
        "day",
    )

    assert not weak.approved
    assert weak.direction == "NO TRADE"
    assert weak.grade == "C"
    assert any(
        "QUALITY_BELOW_THRESHOLD" in reason
        for reason in weak.reasons
    )


def test_director_falls_through_to_the_next_playbook_and_respects_data_health(
    rigged,
):
    make, state = rigged

    state["playbooks"] = [
        Playbook(
            "sweep_reversal",
            "SHORT",
            0.9,
            [],
            101.0,
            92.0,
        ),
        Playbook(
            "trend_pullback",
            "LONG",
            0.8,
            [],
            98.0,
            108.0,
        ),
    ]

    analysis = make(team()).analyze(
        "SOLUSDT",
        "day",
    )

    assert analysis.approved
    assert analysis.direction == "LONG"
    assert analysis.playbook.name == "trend_pullback"

    blocked = make(team()).analyze(
        "SOLUSDT",
        "day",
        data_ok=False,
    )

    assert not blocked.approved
    assert blocked.direction == "NO TRADE"
    assert blocked.reasons[0].startswith(
        "DATA_BLOCKED"
    )


def test_low_signal_agents_never_make_up_the_agreement_count():
    reports = {
        name: rep(name)
        for name in (
            "trend",
            "structure",
            "liquidity",
            "external",
            "derivatives",
        )
    }

    result = consensus(
        reports,
        AGENT_WEIGHTS,
        BULLISH,
        DEFAULT_THRESHOLDS,
    )

    assert any(
        "TOO_FEW_AGENTS (3 < 4" in reason
        for reason in result["reasons"]
    )

    assert "external" in result["agree"]


def test_threshold_overrides_are_validated():
    assert profile(
        "RANGE",
        overrides={"min_agree": 6},
    )["thresholds"]["min_agree"] == 6

    with pytest.raises(PolicyError):
        profile(
            "RANGE",
            overrides={"nonsense": 1},
        )


# TEST_CORE_PY_END
