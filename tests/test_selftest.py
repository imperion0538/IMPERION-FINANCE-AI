import pytest

from imperion.selftest import SCENARIOS


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario(name):
    SCENARIOS[name]()


def test_break_even_move_survives_duplicate_closeposition_rejection():
    """Binance rejects a 2nd closePosition order in the same direction (-4130); the stop swap must fall back to quantity+reduceOnly."""
    from imperion.fakes import make_engine
    from imperion.selftest import SYMBOL, _open
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    fake.set_price(SYMBOL, setup.tp1 * 1.0005)
    bot.lifecycle.manage()
    stops = [a for a in fake.algos if a["orderType"] == "STOP_MARKET"]
    assert len(stops) == 1 and stops[0]["closePosition"] is False and stops[0]["reduceOnly"] is True
    assert float(stops[0]["triggerPrice"]) > bot.db.get_trade(t["id"])["entry"]
    assert any(c[1] == "/fapi/v1/algoOrder" and c[0] == "DELETE" for c in fake.calls)
    bot.stop()


def test_long_holds_fetch_exit_fills_in_windows():
    from imperion.fakes import make_engine
    from imperion.selftest import SYMBOL, _open
    from imperion.util import now_ms
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    fake.calls.clear()
    bot.exec.exit_summary(SYMBOL, now_ms() - 10 * 86_400_000, "LONG")
    windows = [(int(c[2]["startTime"]), int(c[2]["endTime"])) for c in fake.calls if c[1] == "/fapi/v1/userTrades"]
    assert len(windows) >= 2 and all(e - s <= 7 * 86_400_000 for s, e in windows)
    bot.stop()


def test_malformed_positions_payload_never_closes_live_trades():
    from imperion.execution.base import ExecutionError
    from imperion.fakes import make_engine
    from imperion.selftest import _open
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    real = bot.exec.rest.request
    bot.exec.rest.request = lambda method, path, *a, **k: {"code": 0} if "positionRisk" in path else real(method, path, *a, **k)
    with pytest.raises(ExecutionError):
        bot.exec.positions()
    stops_before = len(fake.algos)
    assert bot.lifecycle.manage() == []
    assert bot.db.get_trade(t["id"])["status"] == "OPEN" and len(fake.algos) == stops_before, "protective orders must stay untouched"
    bot.exec.rest.request = real
    bot.stop()


def test_unverifiable_exit_is_closed_without_inventing_pnl_or_loss_streak():
    from imperion.execution.base import ExitSummary
    from imperion.execution.lifecycle import FINALIZE_RETRIES
    from imperion.fakes import make_engine
    from imperion.selftest import SYMBOL, _open
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    bot.exec.exit_summary = lambda *a, **k: ExitSummary(0, 0.0, 0.0, 0.0, complete=False)
    fake.set_price(SYMBOL, setup.stop * 0.999)                                # stop fires, position flat
    for _ in range(FINALIZE_RETRIES - 1):
        bot.lifecycle.manage()
        assert bot.db.get_trade(t["id"])["status"] == "OPEN"                   # still waiting for fills
    bot.lifecycle.manage()
    done = bot.db.get_trade(t["id"])
    assert done["status"] == "CLOSED" and done["exit_reason"] == "EXIT_UNVERIFIED" and done["pnl_usdt"] is None
    assert (bot.db.kv_get("risk.consec_losses", 0) or 0) == 0
    bot.stop()


def test_routine_closes_do_not_raise_critical_alerts():
    from imperion.fakes import make_engine
    from imperion.selftest import _open
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    assert bot.flatten()
    levels = {e["level"] for e in bot.db.events_since(0) if e["kind"] in ("close_order", "emergency_close")}
    assert levels == {"INFO"}
    bot.stop()
