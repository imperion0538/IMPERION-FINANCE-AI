"""Real liquidation integration: exchange leverage brackets, isolated liquidation math, sizing and guardian checks."""
import pytest

from imperion.execution.binance import BinanceExecutor
from imperion.risk.liquidation import (DEFAULT_BRACKETS, assess, bracket_for, isolated_liquidation_price, parse_brackets,
                                       stop_beyond_liquidation)
from imperion.fakes import FakeBinance, make_engine
from imperion.selftest import SYMBOL, _open, _setup

from .helpers import crafted_analyze

ALT = [{"bracket": 1, "initialLeverage": 75, "notionalCap": 5000, "notionalFloor": 0, "maintMarginRatio": 0.01, "cum": 0.0},
       {"bracket": 2, "initialLeverage": 50, "notionalCap": 50000, "notionalFloor": 5000, "maintMarginRatio": 0.015, "cum": 25.0}]


def test_isolated_liquidation_formula_matches_hand_calculation():
    # long: (WB + cum - Q*EP) / (Q*MMR - Q) with WB = Q*EP/lev
    assert isolated_liquidation_price("LONG", 100.0, 1.0, 3, 0.01) == pytest.approx((100 / 3 - 100) / (0.01 - 1))
    assert isolated_liquidation_price("SHORT", 100.0, 1.0, 3, 0.01) == pytest.approx((100 / 3 + 100) / (0.01 + 1))
    with_cum = isolated_liquidation_price("LONG", 100.0, 200.0, 3, 0.015, 25.0)    # cum is deducted from maintenance margin
    assert with_cum < isolated_liquidation_price("LONG", 100.0, 200.0, 3, 0.015, 0.0)
    with pytest.raises(ValueError):
        isolated_liquidation_price("LONG", 100.0, 0.0, 3, 0.01)


def test_brackets_are_parsed_and_selected_by_notional():
    data = [{"symbol": "SOLUSDT", "brackets": ALT}]
    br = parse_brackets(data, "SOLUSDT")
    assert br == parse_brackets({"symbol": "SOLUSDT", "brackets": ALT}, "SOLUSDT")
    assert bracket_for(br, 100)["bracket"] == 1 and bracket_for(br, 5000)["bracket"] == 2 and bracket_for(br, 1e9)["bracket"] == 2
    with pytest.raises(ValueError):
        parse_brackets([{"symbol": "ETHUSDT", "brackets": ALT}], "SOLUSDT")


def test_assessment_refuses_stops_at_or_beyond_liquidation_and_excess_leverage():
    ok = assess("LONG", 100.0, 97.0, 1.0, 3, ALT, 0.7)
    assert ok["ok"] and ok["source"] == "exchange" and ok["liq_price"] < 97.0 and ok["stop_to_liq"] < 0.7
    far = assess("LONG", 100.0, 70.0, 1.0, 3, ALT, 0.7)
    assert not far["ok"] and "STOP_TOO_CLOSE_TO_LIQUIDATION" in far["reasons"][0]
    lev = assess("SHORT", 100.0, 101.0, 100.0, 60, ALT, 0.7)                        # 10k notional -> bracket 2 allows 50x
    assert not lev["ok"] and any("LEVERAGE_ABOVE_BRACKET" in r for r in lev["reasons"])
    assert assess("LONG", 100.0, 97.0, 1.0, 3, None, 0.7)["source"] == "default" and DEFAULT_BRACKETS[0]["maintMarginRatio"] == 0.02
    assert stop_beyond_liquidation("LONG", 95.0, 96.0) and not stop_beyond_liquidation("LONG", 97.0, 96.0)
    assert stop_beyond_liquidation("SHORT", 104.0, 103.9) and not stop_beyond_liquidation("SHORT", 104.0, 0.0)


def test_executor_reads_and_caches_exchange_brackets():
    for single in (False, True):
        bot, fake = make_engine()
        fake.bracket_single_object = single
        br = bot.exec.leverage_brackets("SOLUSDT")
        assert br[0]["maintMarginRatio"] == 0.01 and br[1]["cum"] == 25.0
        n = sum(1 for c in fake.calls if c[1] == "/fapi/v1/leverageBracket")
        bot.exec.leverage_brackets("SOLUSDT")
        assert sum(1 for c in fake.calls if c[1] == "/fapi/v1/leverageBracket") == n, "cached"
        assert isinstance(bot.exec, BinanceExecutor)
        bot.stop()


def test_sizing_uses_exchange_brackets_and_planned_liquidation_matches_the_exchange():
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    assert sizing["liquidation"]["source"] == "exchange" and sizing["liq_price"] < setup.stop
    meta = bot.db.get_trade(t["id"])["meta"]
    assert meta["liq_exchange"] == pytest.approx(meta["liq_planned"], rel=2e-3), "our formula reproduces the exchange's figure"
    bot.stop()
    bot, fake = make_engine(env={"LEVERAGE": "20", "MAX_LEVERAGE": "20"})
    wide = _setup(fake, stop_pct=0.045)                                              # 4.5% stop at 20x: liquidation comes first
    acct = bot.exec.account()
    res = bot.risk.size(wide, bot.exec.filters(SYMBOL), 100.0, acct.available, bot.exec.leverage_brackets(SYMBOL))
    assert not res["ok"] and "LIQUIDATION" in res["reason"]
    bot.stop()


def test_entry_is_blocked_without_exchange_brackets(monkeypatch):
    bot, fake = make_engine()
    monkeypatch.setattr(bot.director, "analyze", crafted_analyze(fake))
    fake.inject("GET", "/fapi/v1/leverageBracket", times=10, status=500, code=-1000, msg="down")
    r = bot.evaluate("SOLUSDT", "day")
    assert not r["traded"] and any("LEVERAGE_BRACKETS_UNAVAILABLE" in x for x in r["reasons"]) and not fake.pos
    bot.stop()


def test_exchange_liquidation_inside_the_stop_closes_the_position():
    bot, fake = make_engine()
    s = _setup(fake)
    fake.liq_override[SYMBOL] = s.stop * 1.001                                       # exchange reports liquidation above the stop
    setup, sizing, t = _open(bot, fake)
    assert not fake.pos and any(e["kind"] == "stop_beyond_liquidation" for e in bot.db.events_since(0))
    assert bot.db.get_trade(t["id"])["exit_reason"] == "STOP_BEYOND_LIQUIDATION"
    bot.stop()
    bot, fake = make_engine(FakeBinance())
    setup, sizing, t = _open(bot, fake)
    assert fake.pos
    fake.liq_override[SYMBOL] = setup.stop * 1.01                                    # margin changed later: guardian reacts
    bot.lifecycle.manage()
    assert SYMBOL not in fake.pos
    bot.stop()
