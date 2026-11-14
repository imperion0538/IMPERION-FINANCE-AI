"""Self-test scenarios run against the strict in-process FakeBinance (no network, no real orders).

`python -m imperion selftest` runs all of them; the pytest suite imports the same functions.
They exercise the REAL production code paths (HMAC signing, Algo-Service orders, lifecycle, risk, reports).
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Dict, List, Tuple

from .fakes import FakeBinance, make_engine, stop_all_engines
from .models import TradeSetup

SYMBOL = "SOLUSDT"


def _setup(fake: FakeBinance, side: str = "LONG", symbol: str = SYMBOL, stop_pct: float = 0.02, style: str = "day") -> TradeSetup:
    px = fake.price(symbol)
    sign = 1 if side == "LONG" else -1
    dist = px * stop_pct
    return TradeSetup(symbol, style, side, px, px - sign * dist, px + sign * 1.2 * dist, px + sign * 2.5 * dist, dist / 2, 2.5, stop_pct * 100, ["selftest"])


def _open(bot, fake, side="LONG", symbol=SYMBOL, **kw):
    setup = _setup(fake, side, symbol, **kw)
    acct = bot.exec.account()
    sizing = bot.risk.size(setup, bot.exec.filters(symbol), bot.risk.trading_equity(acct.equity), acct.available,
                           bot.exec.leverage_brackets(symbol))
    assert sizing["ok"], sizing
    trade = bot.lifecycle.open_trade(setup, sizing, None, {"selftest": True})
    return setup, sizing, trade


def _algos(fake, symbol=SYMBOL) -> List[dict]:
    return [a for a in fake.algos if a["symbol"] == symbol]


# --------------------------------------------------------------------------- scenarios
def preflight_and_key_safety() -> None:
    bot, fake = make_engine()
    assert bot.preflight_result["ok"], bot.preflight_result
    bot.stop()
    bad, fake2 = make_engine(FakeBinance(withdrawals=True))
    assert not bad.preflight_result["ok"]
    assert any("WITHDRAWAL" in e for e in bad.preflight_result["errors"]), bad.preflight_result
    ok, why = bad.can_trade()
    assert not ok and "PREFLIGHT" in why
    r = bad.evaluate(SYMBOL, "day")
    assert not r["traded"]
    bad.stop()


def live_gate_is_closed_without_confirmation() -> None:
    fake = FakeBinance()
    bot, _ = make_engine(fake, env={"LIVE_TRADING_CONFIRM": ""})
    assert bot.exec is None and "OBSERVE" in bot.can_trade()[1]
    assert not any(c[0] == "POST" for c in fake.calls), "observe-only mode must never POST"
    assert not any("/fapi/v3" in c[1] or "/sapi" in c[1] for c in fake.calls), "observe-only must not even read the account"
    bot.stop()
    nokey, _ = make_engine(FakeBinance(), env={"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""})
    assert nokey.exec is None
    nokey.stop()


def trade_reaches_targets() -> None:
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    assert t["status"] == "OPEN", t
    algos = _algos(fake)
    stops = [a for a in algos if a["orderType"] == "STOP_MARKET"]
    tps = [a for a in algos if a["orderType"] == "TAKE_PROFIT_MARKET"]
    assert len(stops) == 1 and stops[0]["closePosition"] is True and stops[0]["workingType"] == "MARK_PRICE", algos
    assert len(tps) == 2, algos
    # a conditional order must NEVER go through the legacy endpoint
    assert not any(c[1] == "/fapi/v1/order" and c[2].get("type", "MARKET") != "MARKET" for c in fake.calls)
    fake.set_price(SYMBOL, setup.tp1 * 1.0005)
    bot.lifecycle.manage()
    t = bot.db.get_trade(t["id"])
    assert t["meta"].get("be_moved"), t
    stop_now = [a for a in _algos(fake) if a["orderType"] == "STOP_MARKET"]
    assert len(stop_now) == 1 and float(stop_now[0]["triggerPrice"]) > t["entry"], stop_now
    fake.set_price(SYMBOL, setup.tp2 * 1.001)
    bot.lifecycle.manage()
    t = bot.db.get_trade(t["id"])
    assert t["status"] == "CLOSED" and t["pnl_usdt"] > 0 and t["r_multiple"] > 1.0, t
    assert not _algos(fake), "orphan protective orders must be cancelled"
    assert not fake.pos
    bot.stop()


def trade_hits_stop() -> None:
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake, "SHORT")
    fake.set_price(SYMBOL, setup.stop * 1.001)
    bot.lifecycle.manage()
    t = bot.db.get_trade(t["id"])
    assert t["status"] == "CLOSED" and t["pnl_usdt"] < 0, t
    assert -1.8 < t["r_multiple"] < -0.6, t["r_multiple"]
    assert bot.db.kv_get("risk.consec_losses") == 1
    assert not _algos(fake)
    bot.stop()


def stop_failure_closes_position() -> None:
    bot, fake = make_engine()
    fake.inject("POST", "/fapi/v1/algoOrder", times=6, status=400, code=-1111, msg="boom")
    setup, sizing, t = _open(bot, fake)
    assert not fake.pos, "position must be flattened when no stop can be placed"
    assert t["status"] in ("CLOSING", "CLOSED"), t
    assert any(e["kind"] == "emergency_close" for e in bot.db.events_since(0)), "emergency close must be logged"
    bot.lifecycle.manage()
    assert bot.db.get_trade(t["id"])["status"] == "CLOSED"
    bot.stop()


def entry_timeout_is_recovered() -> None:
    bot, fake = make_engine()
    fake.inject("POST", "/fapi/v1/order", times=1, timeout=True, execute_first=True)
    setup, sizing, t = _open(bot, fake)
    assert t["status"] == "OPEN" and abs(fake.pos[SYMBOL]["qty"] - float(sizing["qty"])) < 1e-9, (t, fake.pos)
    assert _algos(fake), "stop must still be placed after a recovered entry"
    bot.stop()


def guardian_recreates_missing_stop() -> None:
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    fake.algos = [a for a in fake.algos if a["orderType"] != "STOP_MARKET"]     # someone deleted the stop
    bot.lifecycle.manage()
    assert any(a["orderType"] == "STOP_MARKET" for a in fake.algos), "guardian must re-create the stop"
    bot.stop()


def daily_loss_halts_and_clears_next_day() -> None:
    # Test opt-in legacy loss/drawdown behavior; defaults remain disabled.
    bot, fake = make_engine(env={"DAILY_LOSS_LIMIT_PCT": "1.5", "MAX_DRAWDOWN_PCT": "5"})
    bot.risk.update_equity(100.0)
    reason = bot.risk.update_equity(96.5)
    assert reason and "daily loss" in reason and bot.risk.halted()
    ok, why = bot.can_trade()
    assert not ok and "HALTED" in why
    bot.db.kv_set("risk.day", "1999-01-01")            # pretend a new UTC day started
    bot.risk.update_equity(96.5)
    assert not bot.risk.halted(), "daily-loss halt should lapse on the next UTC day"
    bot.risk.update_equity(80.0)                        # 17% below peak -> drawdown halt does NOT auto-clear
    assert "drawdown" in (bot.risk.halted() or "")
    bot.db.kv_set("risk.day", "1999-01-01")
    bot.risk.update_equity(80.0)
    assert bot.risk.halted()
    bot.stop()


def report_renders_and_sends() -> None:
    sent: List[Tuple[str, str]] = []
    bot, fake = make_engine(env={"SMTP_HOST": "smtp.test", "MAIL_TO": "me@example.com", "MAIL_FROM": "bot@example.com"},
                         mailer=lambda cfg, subject, text, html: (sent.append((subject, text)) or (True, "sent")))
    _open(bot, fake)
    ok, detail = bot.send_report()
    assert ok and sent and "IMPERION" in sent[0][0] and "AÇIK POZİSYONLAR" in sent[0][1], sent
    assert "BTCUSDT" in sent[0][1]
    bot.stop()


SCENARIOS: Dict[str, Callable[[], None]] = {
    "preflight_and_key_safety": preflight_and_key_safety,
    "live_gate_closed_without_confirmation": live_gate_is_closed_without_confirmation,
    "trade_reaches_targets": trade_reaches_targets,
    "trade_hits_stop": trade_hits_stop,
    "stop_failure_closes_position": stop_failure_closes_position,
    "entry_timeout_is_recovered": entry_timeout_is_recovered,
    "guardian_recreates_missing_stop": guardian_recreates_missing_stop,
    "daily_loss_halts_and_clears_next_day": daily_loss_halts_and_clears_next_day,
    "report_renders_and_sends": report_renders_and_sends,
}


def run_all(verbose: bool = True) -> int:
    failures = 0
    for name, fn in SCENARIOS.items():
        t0 = time.time()
        try:
            fn()
            status = "PASS"
        except Exception as exc:  # noqa: BLE001
            failures += 1
            status = f"FAIL: {type(exc).__name__}: {exc}"
        if verbose:
            print(f"  [{status[:4]}] {name} ({time.time() - t0:.1f}s)" + ("" if status == "PASS" else "\n        " + status))
    stuck = stop_all_engines()              # a failed scenario must not leave threads behind
    if stuck:
        failures += 1
        if verbose:
            print(f"  [FAIL] threads still running after the scenarios: {stuck}")
    if verbose:
        print("selftest:", "ALL PASSED" if not failures else f"{failures} FAILED")
    return failures


if __name__ == "__main__":  # pragma: no cover
    sys.exit(1 if run_all() else 0)
