"""Safety regressions carried over from earlier releases (exposure, reconciliation, risk baselines, version)."""
from pathlib import Path

import pytest

from imperion.config import STYLES, VERSION
from imperion.execution.base import ExecutionError
from imperion.fakes import make_engine
from imperion.selftest import SYMBOL, _setup
from imperion.util import now_ms



def test_version_file_matches_package_version():
    text = (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8")
    assert text == VERSION + "\n", "VERSION must hold exactly the package version followed by one newline"


# ---- exposure / reconciliation -------------------------------------------------------------------------
def _break_positions(bot):
    def boom():
        raise ExecutionError("positionRisk unavailable")
    bot.exec.positions = boom
    bot.lifecycle._pos_cache = (0.0, {})


def test_unreadable_positions_block_entries_without_crashing_and_halt_only_when_persistent():
    bot, fake = make_engine()
    _break_positions(bot)
    exposure = bot.lifecycle.exposure()                    # used to raise AttributeError ('dict' has no attribute 'symbol')
    assert [e["symbol"] for e in exposure] == ["__EXPOSURE_UNKNOWN__"]
    pre = bot.risk.precheck(SYMBOL, STYLES["day"], "LONG", exposure, 100.0)
    assert not pre["allowed"] and not pre["position_limit_ok"] and any("EXPOSURE_UNKNOWN" in r for r in pre["reasons"])
    assert not bot.risk.halted(), "a single failed query must not halt trading permanently"
    bot.lifecycle._exposure_fail_since -= 120              # ... but a persistent outage must
    bot.lifecycle.exposure()
    assert "exposure unknown" in (bot.risk.halted() or "")
    bot.stop()


def test_entry_unknown_is_adopted_when_the_position_exists():
    bot, fake = make_engine()
    setup = _setup(fake)
    fake.pos[SYMBOL] = {"qty": 0.5, "entry": setup.entry}
    bot.db.upsert_trade({"id": "tu1", "ts_open": now_ms() - 60_000, "symbol": SYMBOL, "style": "day", "side": "LONG", "entry": setup.entry,
                         "qty": 0.5, "stop": setup.stop, "tp1": setup.tp1, "tp2": setup.tp2, "risk_usdt": 1.0, "status": "ENTRY_UNKNOWN",
                         "mode": "live", "meta": {"plan_entry": setup.entry}})
    bot.lifecycle.manage()
    t = bot.db.get_trade("tu1")
    assert t["status"] == "OPEN" and t["meta"]["plan_entry"] == setup.entry
    assert any(a["orderType"] == "STOP_MARKET" for a in fake.algos), "an adopted position must get its protective stop"
    bot.stop()


def test_entry_unknown_without_position_is_discarded():
    bot, fake = make_engine()
    setup = _setup(fake)
    bot.db.upsert_trade({"id": "tu2", "ts_open": now_ms() - 60_000, "symbol": SYMBOL, "style": "day", "side": "LONG", "entry": setup.entry,
                         "qty": 0.5, "stop": setup.stop, "tp1": setup.tp1, "tp2": setup.tp2, "risk_usdt": 1.0, "status": "ENTRY_UNKNOWN", "mode": "live"})
    bot.lifecycle.manage()
    t = bot.db.get_trade("tu2")
    assert t["status"] == "FAILED" and t["exit_reason"] == "ENTRY_NOT_FOUND" and not bot.db.open_trades()
    bot.stop()


def test_failed_entry_keeps_trade_meta():
    bot, fake = make_engine()
    fake.inject("POST", "/fapi/v1/order", times=1, status=400, code=-2019, msg="Margin is insufficient")
    setup, sizing = _setup(fake), None
    acct = bot.exec.account()
    sizing = bot.risk.size(setup, bot.exec.filters(SYMBOL), bot.risk.trading_equity(acct.equity), acct.available)
    t = bot.lifecycle.open_trade(setup, sizing, None, {"source": "test"})
    assert t["status"] == "FAILED" and t["meta"]["source"] == "test" and not fake.pos
    bot.stop()


# ---- risk baseline -------------------------------------------------------------------------------------
def test_resume_after_drawdown_does_not_rehalt_when_wallet_exceeds_allocation():
    # Optional drawdown policy is OFF by default for Day Trade.
    bot, fake = make_engine(env={"MAX_DRAWDOWN_PCT": "5", "DAILY_LOSS_LIMIT_PCT": "0"})
    risk = bot.risk
    risk.update_equity(1000.0)                             # wallet 1000, allocation 100
    assert "drawdown" in (risk.update_equity(980.0) or "")  # -20 USDT = -20% of the allocation
    risk.resume(equity=980.0, by="admin")
    assert risk.update_equity(980.0) is None and not risk.halted()
    assert risk.status()["peak_equity"] == pytest.approx(80.0), "baselines are kept in allocated-equity units"
    bot.stop()


def test_cli_style_resume_without_equity_rebaselines_on_next_update():
    bot, fake = make_engine(env={"MAX_DRAWDOWN_PCT": "5", "DAILY_LOSS_LIMIT_PCT": "0"})
    risk = bot.risk
    risk.update_equity(100.0)
    assert risk.update_equity(80.0)
    risk.resume(by="cli")
    assert risk.update_equity(80.0) is None and not risk.halted()
    bot.stop()


def test_blocked_decision_explains_the_risk_reason(monkeypatch):
    from .helpers import crafted_analyze
    bot, fake = make_engine()
    monkeypatch.setattr(bot.director, "analyze", crafted_analyze(fake))
    assert bot.evaluate("SOLUSDT", "day")["traded"]
    again = bot.evaluate("SOLUSDT", "day")
    assert again["status"] == "BLOCKED" and any("SYMBOL_ALREADY_HAS_POSITION" in r for r in again["reasons"]) and again["setup"]
    bot.stop()
