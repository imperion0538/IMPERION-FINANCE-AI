
"""Runtime engine against the strict fake exchange.

The Director is replaced by a crafted, approved analysis so these
tests exercise the gates, risk, execution and persistence paths
deterministically.
"""

import json
import threading
import time

import pytest

from imperion.execution.sim import SimExecutor
from imperion.fakes import FakeBinance, make_engine
from imperion.util import now_ms

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

    r = engine.evaluate("SOLUSDT", "day")
    assert r["traded"] and r["status"] == "TRADED", r

    t = trades(engine)[0]
    assert (
        t["status"] == "OPEN"
        and t["style"] == "day"
        and t["side"] == "LONG"
        and t["mode"] == "live"
    )

    # Conservative 0.25% default risk.
    assert 0.15 <= t["risk_usdt"] <= 0.30

    kinds = sorted(
        (a["orderType"], a["closePosition"])
        for a in fake.algos
    )
    assert kinds == [
        ("STOP_MARKET", True),
        ("TAKE_PROFIT_MARKET", False),
        ("TAKE_PROFIT_MARKET", False),
    ]

    dec = engine.db.one(
        "SELECT * FROM decisions WHERE status='TRADED'"
    )
    setup = json.loads(dec["setup_json"])

    assert (
        dec["approved"] == 1
        and setup["stop"] < setup["entry"]
        and setup["playbook"] == "trend_pullback"
        and setup["grade"] == "A"
    )

    detail = json.loads(dec["evidence_json"])
    assert (
        detail["agents"]["trend"]["d"] == "BULLISH"
        and detail["playbook"]["name"] == "trend_pullback"
    )

    shadow = engine.db.one("SELECT * FROM shadows")
    assert shadow is not None

    shadow_meta = json.loads(shadow["meta_json"])

    # V17 Unified Institutional core identity.
    assert (
        shadow_meta["core"]
        == "agents-1-v17-unified-institutional"
    )
    assert shadow_meta["playbook"] == "trend_pullback"

    meta = json.loads(t["meta_json"])
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
        "SYMBOL_ALREADY" in x
        or "COOLDOWN" in x
        or "OPEN_RISK" in x
        for x in again["reasons"]
    )

    second_symbol = engine.evaluate("SOLUSDT", "day")
    assert not second_symbol["traded"]

    third = engine.evaluate("SOLUSDT", "day")
    assert not third["traded"]

    assert len(trades(engine)) == 1


def test_never_trades_with_unhealthy_data(rig):
    engine, fake = rig

    for conn in (engine.streams.market, engine.streams.public):
        conn.connected = False

    r = engine.evaluate("SOLUSDT", "day")

    assert (
        not r["traded"]
        and r["status"] == "DATA_BLOCKED"
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

    e2, _ = make_engine(FakeBinance(withdrawals=True))
    assert not e2.can_trade()[0]
    e2.stop()


def test_observe_only_never_posts(monkeypatch):
    engine, fake = make_engine(
        env={"LIVE_TRADING_CONFIRM": ""}
    )

    monkeypatch.setattr(
        engine.director,
        "analyze",
        crafted_analyze(fake),
    )

    r = engine.evaluate("SOLUSDT", "day")

    assert r["status"] == "APPROVED_NOT_TRADED"
    assert any(
        "OBSERVE_ONLY" in x for x in r["reasons"]
    )

    assert not [
        c for c in fake.calls
        if c[0] in ("POST", "DELETE")
    ]

    assert engine.db.one(
        "SELECT COUNT(*) c FROM shadows"
    )["c"] == 1, "learning continues without trading"

    engine.stop()


def test_parallel_signals_open_only_one_position(rig):
    engine, fake = rig
    out = []

    ts = [
        threading.Thread(
            target=lambda: out.append(
                engine.evaluate("SOLUSDT", "day")
            )
        )
        for _ in range(4)
    ]

    for t in ts:
        t.start()

    for t in ts:
        t.join()

    assert sum(
        1 for r in out if r["traded"]
    ) == 1

    assert len(trades(engine)) == 1

    assert abs(fake.pos["SOLUSDT"]["qty"]) == pytest.approx(
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

    r = engine.evaluate(
        "LINKUSDT",
        "swing",
        hint="SHORT",
        source="tradingview",
    )

    assert r["traded"], r["reasons"]

    t = trades(engine)[0]
    assert (
        t["side"] == "SHORT"
        and t["stop"] > t["entry"] > t["tp2"]
        and json.loads(t["meta_json"])["source"]
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

    r = engine.evaluate("SOLUSDT", "day")

    assert (
        r["traded"]
        and r["ai"]["p_win"] < 0.1
        and not r["ai"]["blocked"]
    )

    engine.learning.model.validated = True

    r = engine.evaluate("LTCUSDT", "day")

    assert (
        not r["traded"]
        and r["status"] == "BLOCKED"
        and any(
            "AI_LOW_WIN_PROBABILITY" in x
            for x in r["reasons"]
        )
    )

    assert engine.db.one(
        "SELECT COUNT(*) c FROM shadows"
    )["c"] == 2, "vetoed setups stay shadow-tracked"


def test_decision_pass_runs_each_series_once_per_candle(
    rig,
    monkeypatch,
):
    engine, fake = rig
    calls = []

    monkeypatch.setattr(
        engine,
        "evaluate",
        lambda sym, style, hint=None,
        source="loop", now=None:
        calls.append((sym, style)) or {},
    )

    n = engine.decision_pass()

    assert (
        n
        == len(engine.cfg.symbols)
        * len(engine.cfg.active_styles)
        == len(calls)
    )

    assert engine.decision_pass() == 0, (
        "no new closed candle -> no re-evaluation"
    )


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

    t = trades(engine)[0]
    db = engine.db

    engine.stop()

    e2, _ = make_engine(fake, db=db)

    assert len(e2.db.open_trades()) == 1
    assert "SOLUSDT" in fake.pos

    fake.set_price(
        "SOLUSDT",
        t["stop"] * 0.999,
    )

    e2.lifecycle.manage()

    closed = e2.db.get_trade(t["id"])

    assert (
        closed["status"] == "CLOSED"
        and closed["pnl_usdt"] < 0
        and not fake.algos
    )

    e2.stop()


def test_foreign_positions_are_counted_but_never_touched(rig):
    engine, fake = rig

    fake.pos["DOTUSDT"] = {
        "qty": 50.0,
        "entry": fake.price("DOTUSDT"),
    }

    engine.lifecycle._pos_cache = (0.0, {})

    exposure = engine.lifecycle.exposure()

    assert any(
        e["symbol"] == "DOTUSDT"
        and e["meta"].get("foreign")
        for e in exposure
    )

    engine.lifecycle.manage()

    assert "DOTUSDT" in fake.pos

    assert any(
        e["kind"] == "foreign_position"
        for e in engine.db.events_since(0)
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

    assert isinstance(engine.exec, SimExecutor)

    monkeypatch.setattr(
        engine.director,
        "analyze",
        crafted_analyze(fake),
    )

    px = fake.price("SOLUSDT")

    engine.trades.add(
        "SOLUSDT",
        now_ms(),
        px,
        10,
        False,
    )

    r = engine.evaluate("SOLUSDT", "day")
    assert r["traded"], r["reasons"]

    t = trades(engine)[0]
    assert t["mode"] == "dry"

    engine.trades.add(
        "SOLUSDT",
        now_ms() + 1,
        t["tp2"] * 1.002,
        10,
        False,
    )

    engine.manage_pass()
    engine.manage_pass()

    done = engine.db.get_trade(t["id"])

    assert (
        done["status"] == "CLOSED"
        and done["pnl_usdt"] > 0
    ), done

    assert not [
        c for c in fake.calls
        if c[0] == "POST"
    ], "dry mode must never POST to the exchange"

    engine.stop()


def test_critical_events_are_mailed_with_rate_limit(rig):
    engine, fake = rig
    sent = []

    engine.mailer = (
        lambda cfg, subject, text, html:
        (sent.append((subject, text)) or (True, "sent"))
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

    assert set(engine.radar.symbols()).isdisjoint(
        engine.cfg.symbols
    ), "radar never overlaps the execution universe"

    assert all(
        engine.candles.count(s, "1h") >= 100
        for s in engine.radar.symbols()
    ), "radar symbols get 1h history"

    snap = engine.snapshot()

    assert (
        set(snap["symbols"]) == set(engine.cfg.symbols)
        and snap["radar"]
        and snap["radar"][0]["bias_1h"]
    )

    st = engine.status()

    assert (
        st["trading_enabled"]
        and st["equity"]["equity"]
        == pytest.approx(100.0, rel=0.01)
        and st["core"].startswith("IMPERION Director")
    )

    dumped = (
        json.dumps(st, default=str)
        + json.dumps(engine.health(), default=str)
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
    r = engine.evaluate("SOLUSDT", "day")

    assert (
        r["status"] in (
            "NO_TRADE",
            "BLOCKED",
            "TRADED",
            "APPROVED",
            "APPROVED_NOT_TRADED",
        )
        and r["reasons"]
    )

    assert set(r["agents"])
    assert r["mtf"]["tfs"]

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

    engine.start(http=False, threads=True)

    try:
        time.sleep(6)

        h = engine.health()

        assert h["ready"] and h["ws_healthy"], h

        assert all(
            t.is_alive()
            for t in engine._threads
        ), [
            t.name
            for t in engine._threads
            if not t.is_alive()
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
