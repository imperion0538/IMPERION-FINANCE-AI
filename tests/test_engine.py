"""Runtime engine against the strict fake exchange.

The Director is replaced by a crafted, approved analysis so these
tests exercise the gates, risk, execution and persistence paths.

Decision-loop timing uses a controlled application clock.
Production data-freshness checks are preserved.
"""

import json
import threading
import time

import pytest

from imperion.config import STYLES
from imperion.execution.sim import SimExecutor
from imperion.fakes import FakeBinance, make_engine
from imperion.util import last_closed_open, now_ms

from .helpers import crafted_analyze


@pytest.fixture()
def rig(monkeypatch):
    engine, fake = make_engine()

    monkeypatch.setattr(
        engine.director,
        "analyze",
        crafted_analyze(fake),
    )

    yield engine, fake
    engine.stop()


def trades(engine):
    return engine.db.query(
        "SELECT * FROM trades ORDER BY ts_open"
    )


def test_full_approved_path_places_entry_stop_and_targets(rig):
    engine, fake = rig

    result = engine.evaluate("SOLUSDT", "day")

    assert (
        result["traded"]
        and result["status"] == "TRADED"
    ), result

    trade = trades(engine)[0]

    assert (
        trade["status"] == "OPEN"
        and trade["style"] == "day"
        and trade["side"] == "LONG"
        and trade["mode"] == "live"
    )

    # Requested <=1% risk.
    assert 0.0 < trade["risk_usdt"] <= 1.0

    kinds = sorted(
        (order["orderType"], order["closePosition"])
        for order in fake.algos
    )

    assert kinds == [
        ("STOP_MARKET", True),
        ("TAKE_PROFIT_MARKET", False),
        ("TAKE_PROFIT_MARKET", False),
    ]

    decision = engine.db.one(
        "SELECT * FROM decisions WHERE status='TRADED'"
    )

    setup = json.loads(decision["setup_json"])

    assert (
        decision["approved"] == 1
        and setup["stop"] < setup["entry"]
        and setup["playbook"] == "trend_pullback"
        and setup["grade"] == "A"
    )

    detail = json.loads(decision["evidence_json"])

    assert (
        detail["agents"]["trend"]["d"] == "BULLISH"
        and detail["playbook"]["name"] == "trend_pullback"
    )

    shadow = engine.db.one("SELECT * FROM shadows")
    assert shadow is not None

    shadow_meta = json.loads(shadow["meta_json"])

    assert (
        shadow_meta["core"]
        == "agents-1-v17-unified-institutional"
    )
    assert shadow_meta["playbook"] == "trend_pullback"

    meta = json.loads(trade["meta_json"])

    assert (
        meta["source"] == "loop"
        and meta["playbook"] == "trend_pullback"
        and meta["grade"] == "A"
    )

    assert engine.db.kv_get("risk.daily_new_risk") > 0


def test_second_entry_rules(rig):
    engine, fake = rig

    assert engine.evaluate("SOLUSDT", "day")["traded"]

    again = engine.evaluate("SOLUSDT", "day")

    assert not again["traded"]

    assert any(
        "SYMBOL_ALREADY" in reason
        or "COOLDOWN" in reason
        or "OPEN_RISK" in reason
        for reason in again["reasons"]
    )

    second_symbol = engine.evaluate("SOLUSDT", "day")
    assert not second_symbol["traded"]

    third = engine.evaluate("SOLUSDT", "day")
    assert not third["traded"]

    assert len(trades(engine)) == 1


def test_never_trades_with_unhealthy_data(rig):
    engine, fake = rig

    for connection in (
        engine.streams.market,
        engine.streams.public,
    ):
        connection.connected = False

    result = engine.evaluate("SOLUSDT", "day")

    assert (
        not result["traded"]
        and result["status"] == "DATA_BLOCKED"
        and not trades(engine)
        and not fake.pos
    )

    assert not engine.db.one(
        "SELECT COUNT(*) c FROM shadows"
    )["c"], "no learning from unhealthy data"


def test_halt_and_preflight_gate(rig):
    engine, fake = rig

    engine.halt("manual")

    assert (
        not engine.evaluate("SOLUSDT", "day")["traded"]
        and not trades(engine)
    )

    engine.resume()

    assert engine.evaluate("SOLUSDT", "day")["traded"]

    second_engine, _ = make_engine(
        FakeBinance(withdrawals=True)
    )

    assert not second_engine.can_trade()[0]

    second_engine.stop()


def test_observe_only_never_posts(monkeypatch):
    engine, fake = make_engine(
        env={"LIVE_TRADING_CONFIRM": ""}
    )

    monkeypatch.setattr(
        engine.director,
        "analyze",
        crafted_analyze(fake),
    )

    result = engine.evaluate("SOLUSDT", "day")

    assert result["status"] == "APPROVED_NOT_TRADED"

    assert any(
        "OBSERVE_ONLY" in reason
        for reason in result["reasons"]
    )

    assert not [
        call
        for call in fake.calls
        if call[0] in ("POST", "DELETE")
    ]

    assert engine.db.one(
        "SELECT COUNT(*) c FROM shadows"
    )["c"] == 1, "learning continues without trading"

    engine.stop()


def test_parallel_signals_open_only_one_position(rig):
    engine, fake = rig
    results = []

    threads = [
        threading.Thread(
            target=lambda: results.append(
                engine.evaluate("SOLUSDT", "day")
            )
        )
        for _ in range(4)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert sum(
        1 for result in results if result["traded"]
    ) == 1

    assert len(trades(engine)) == 1

    assert abs(
        fake.pos["SOLUSDT"]["qty"]
    ) == pytest.approx(
        float(trades(engine)[0]["qty"])
    )


def test_short_side_and_tradingview_hint(monkeypatch):
    engine, fake = make_engine()

    monkeypatch.setattr(
        engine.director,
        "analyze",
        crafted_analyze(
            fake,
            "SHORT",
            "sweep_reversal",
        ),
    )

    result = engine.evaluate(
        "LINKUSDT",
        "day",
        hint="SHORT",
        source="tradingview",
    )

    assert result["traded"], result["reasons"]

    trade = trades(engine)[0]

    assert (
        trade["side"] == "SHORT"
        and trade["stop"] > trade["entry"] > trade["tp2"]
        and json.loads(trade["meta_json"])["source"]
        == "tradingview"
    )

    engine.stop()


def test_ai_layer_can_only_block(rig):
    engine, fake = rig

    from imperion.learning.model import WinModel

    engine.learning.model = WinModel(
        {},
        bias=-3.0,
        validated=False,
    )

    result = engine.evaluate("SOLUSDT", "day")

    assert (
        result["traded"]
        and result["ai"]["p_win"] < 0.1
        and not result["ai"]["blocked"]
    )

    engine.learning.model.validated = True

    result = engine.evaluate("LTCUSDT", "day")

    assert (
        not result["traded"]
        and result["status"] == "BLOCKED"
        and any(
            "AI_LOW_WIN_PROBABILITY" in reason
            for reason in result["reasons"]
        )
    )

    assert engine.db.one(
        "SELECT COUNT(*) c FROM shadows"
    )["c"] == 2, "vetoed setups stay shadow-tracked"


@pytest.mark.parametrize(
    "offset_ms",
    (1, 59_999, 299_999),
)
def test_decision_pass_runs_each_series_once_per_candle(
    monkeypatch,
    offset_ms,
):
    """Use one controlled clock for bootstrap and dispatch.

    A real minute boundary during bootstrap can leave only some
    symbols with the latest closed candle. Keep the test input
    deterministic without relaxing production freshness checks.
    """
    import sys

    from imperion.runtime import engine as runtime_engine

    epoch = (
        1_795_000_000_000 // 300_000
    ) * 300_000

    clock = {"now": epoch + offset_ms}

    def controlled_now_ms():
        return clock["now"]

    # Load runtime modules before patching so pytest can restore
    # their imported clock references after this test.
    #
    # Do not patch time.time(): shutdown deadlines and thread
    # timeout accounting must continue using real time.
    for name, module in list(sys.modules.items()):
        if (
            name.startswith("imperion.")
            and hasattr(module, "now_ms")
        ):
            monkeypatch.setattr(
                module,
                "now_ms",
                controlled_now_ms,
            )

    assert runtime_engine.now_ms() == clock["now"]

    engine, fake = make_engine()
    calls = []

    def record_evaluation(
        symbol,
        style,
        hint=None,
        source="loop",
        now=None,
        prepared=None,
    ):
        assert now == clock["now"]
        calls.append((symbol, style))
        return {}

    try:
        monkeypatch.setattr(
            engine,
            "evaluate",
            record_evaluation,
        )

        expected = {
            (symbol, style)
            for symbol in engine.cfg.symbols
            for style in engine.cfg.active_styles
        }

        # Verify the initial data, rather than assuming that
        # every series was bootstrapped before a real boundary.
        for symbol, style in expected:
            trigger_tf = STYLES[style].trigger_tf

            assert engine.candles.last_open(
                symbol,
                trigger_tf,
            ) == last_closed_open(
                clock["now"],
                trigger_tf,
            )

        count = engine.decision_pass()

        assert count == len(expected) == len(calls)
        assert set(calls) == expected

        assert engine.decision_pass() == 0, (
            "no new closed candle -> no re-evaluation"
        )

        assert len(calls) == len(expected)

        # Time passing alone must not authorize stale series.
        clock["now"] += 60_000
        calls.clear()

        assert engine.decision_pass() == 0
        assert calls == []

        # Only the 5m Day Trade trigger is active, never the 1m scalp trigger.
        assert engine.cfg.active_styles == ("day",)
        # Advance to the next 5m closed candle.
        clock["now"] = (
            clock["now"] // 300_000 + 1
        ) * 300_000 + 1

        calls.clear()

        rows = engine.rest.klines(
            "SOLUSDT",
            "5m",
            limit=3,
        )

        engine.candles.ingest(
            "SOLUSDT",
            "5m",
            rows,
            now=clock["now"],
        )

        assert engine.decision_pass() == 1
        assert calls == [("SOLUSDT", "day")]

        assert engine.decision_pass() == 0
        assert calls == [("SOLUSDT", "day")]

    finally:
        engine.stop()


def test_restart_adopts_open_positions_and_finishes_them(
    monkeypatch,
):
    engine, fake = make_engine()

    monkeypatch.setattr(
        engine.director,
        "analyze",
        crafted_analyze(fake),
    )

    assert engine.evaluate("SOLUSDT", "day")["traded"]

    trade = trades(engine)[0]
    db = engine.db

    engine.stop()

    second_engine, _ = make_engine(
        fake,
        db=db,
    )

    assert len(
        second_engine.db.open_trades()
    ) == 1

    assert "SOLUSDT" in fake.pos

    fake.set_price(
        "SOLUSDT",
        trade["stop"] * 0.999,
    )

    second_engine.lifecycle.manage()

    closed = second_engine.db.get_trade(
        trade["id"]
    )

    assert (
        closed["status"] == "CLOSED"
        and closed["pnl_usdt"] < 0
        and not fake.algos
    )

    second_engine.stop()


def test_foreign_positions_are_counted_but_never_touched(rig):
    engine, fake = rig

    fake.pos["DOTUSDT"] = {
        "qty": 50.0,
        "entry": fake.price("DOTUSDT"),
    }

    engine.lifecycle._pos_cache = (0.0, {})

    exposure = engine.lifecycle.exposure()

    assert any(
        row["symbol"] == "DOTUSDT"
        and row["meta"].get("foreign")
        for row in exposure
    )

    engine.lifecycle.manage()

    assert "DOTUSDT" in fake.pos

    assert any(
        event["kind"] == "foreign_position"
        for event in engine.db.events_since(0)
    )

    assert not engine.flatten()
    assert "DOTUSDT" in fake.pos


def test_flatten_closes_only_own_positions_and_halts(rig):
    engine, fake = rig

    assert engine.evaluate("SOLUSDT", "day")["traded"]

    fake.pos["DOTUSDT"] = {
        "qty": 50.0,
        "entry": fake.price("DOTUSDT"),
    }

    assert engine.flatten() == ["SOLUSDT"]

    assert "SOLUSDT" not in fake.pos
    assert "DOTUSDT" in fake.pos
    assert engine.risk.halted()


def test_dry_mode_runs_end_to_end_with_simulated_fills(
    monkeypatch,
):
    engine, fake = make_engine(
        env={
            "MODE": "dry",
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
            "LIVE_TRADING_CONFIRM": "",
        }
    )

    assert isinstance(
        engine.exec,
        SimExecutor,
    )

    monkeypatch.setattr(
        engine.director,
        "analyze",
        crafted_analyze(fake),
    )

    price = fake.price("SOLUSDT")

    engine.trades.add(
        "SOLUSDT",
        now_ms(),
        price,
        10,
        False,
    )

    result = engine.evaluate("SOLUSDT", "day")

    assert result["traded"], result["reasons"]

    trade = trades(engine)[0]

    assert trade["mode"] == "dry"

    engine.trades.add(
        "SOLUSDT",
        now_ms() + 1,
        trade["tp2"] * 1.002,
        10,
        False,
    )

    engine.manage_pass()
    engine.manage_pass()

    done = engine.db.get_trade(
        trade["id"]
    )

    assert (
        done["status"] == "CLOSED"
        and done["pnl_usdt"] > 0
    ), done

    assert not [
        call
        for call in fake.calls
        if call[0] == "POST"
    ], "dry mode must never POST to the exchange"

    engine.stop()


def test_critical_events_are_mailed_with_rate_limit(rig):
    engine, fake = rig
    sent = []

    engine.mailer = (
        lambda cfg, subject, text, html:
        (
            sent.append((subject, text))
            or (True, "sent")
        )
    )

    engine.risk.halt("test halt")
    engine._flush_alert_mail()

    assert (
        len(sent) == 1
        and "KRİTİK" in sent[0][0]
        and "test halt" in sent[0][1]
    )

    engine.risk.halt("second incident")
    engine._flush_alert_mail()

    assert len(sent) == 1

    engine._mail_sent.clear()
    engine._flush_alert_mail()

    assert (
        len(sent) == 2
        and "second incident" in sent[1][1]
    )


def test_maintenance_snapshot_status_and_secrets(rig):
    engine, fake = rig

    engine.maintenance_pass()

    assert engine.deriv.get("SOLUSDT") is not None
    assert engine.radar.symbols()

    assert set(
        engine.radar.symbols()
    ).isdisjoint(
        engine.cfg.symbols
    ), "radar never overlaps the execution universe"

    assert all(
        engine.candles.count(symbol, "1h") >= 100
        for symbol in engine.radar.symbols()
    ), "radar symbols get 1h history"

    snapshot = engine.snapshot()

    assert (
        set(snapshot["symbols"])
        == set(engine.execution_symbols())
        and snapshot["radar"]
        and snapshot["radar"][0]["bias_1h"]
    )

    status = engine.status()

    assert (
        status["trading_enabled"]
        and status["equity"]["equity"]
        == pytest.approx(100.0, rel=0.01)
        and status["core"].startswith(
            "IMPERION Director"
        )
    )

    dumped = (
        json.dumps(status, default=str)
        + json.dumps(
            engine.health(),
            default=str,
        )
    )

    for secret in (
        fake.api_key,
        fake.api_secret,
        engine.cfg.admin_token,
        engine.cfg.webhook_secret,
    ):
        assert secret not in dumped


def test_real_director_path_runs_and_records_decisions():
    """Run the real Director on fake market data."""
    engine, fake = make_engine()

    result = engine.evaluate("SOLUSDT", "day")

    assert (
        result["status"] in (
            "NO_TRADE",
            "BLOCKED",
            "TRADED",
            "APPROVED",
            "APPROVED_NOT_TRADED",
        )
        and result["reasons"]
    )

    assert set(result["agents"])
    assert result["mtf"]["tfs"]

    assert engine.db.one(
        "SELECT COUNT(*) c FROM decisions"
    )["c"] >= 1

    with pytest.raises(ValueError):
        engine.evaluate("PEPEUSDT", "day")

    engine.stop()


def test_threaded_runtime_smoke():
    """Run real threads with fake exchange and WebSocket."""
    engine, fake = make_engine(
        live_ws=True,
        warm=False,
        start=False,
    )

    engine.start(
        http=False,
        threads=True,
    )

    try:
        time.sleep(6)

        health = engine.health()

        assert (
            health["ready"]
            and health["ws_healthy"]
        ), health

        assert all(
            thread.is_alive()
            for thread in engine._threads
        ), [
            thread.name
            for thread in engine._threads
            if not thread.is_alive()
        ]

    finally:
        engine.stop()


def test_health_is_not_ready_until_startup_finishes(rig):
    engine, _ = rig

    engine._ready = False
    health = engine.health()

    assert health["ready"] is False
    assert health["ok"] is False

    engine._ready = True
    health = engine.health()

    assert health["ready"] is True
    assert health["ok"] is True


# TEST_ENGINE_PY_END
