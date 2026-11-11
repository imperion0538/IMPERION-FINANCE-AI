"""Execution layer: maker entries, trailing stop, funding in PnL, liquidation buffer, user data stream."""
import pytest

from imperion.config import load_settings
from imperion.exchange.rest import RestClient
from imperion.exchange.user_stream import UserStream
from imperion.fakes import FakeBinance, idle_ws_factory, make_engine
from imperion.models import TradeSetup
from imperion.selftest import SYMBOL, _open, _setup
from imperion.util import now_ms

TG = {"TELEGRAM_BOT_TOKEN": "123456:SECRET-TOKEN", "TELEGRAM_CHAT_ID": "4242"}


# ---- maker entry ---------------------------------------------------------------------------------------
MAKER = {"ENTRY_MODE": "maker", "ENTRY_MAKER_WAIT_S": "1"}


def _orders(fake, typ):
    return [c for c in fake.calls if c[0] == "POST" and c[1] == "/fapi/v1/order" and c[2].get("type") == typ]


def test_maker_entry_fills_as_post_only_limit():
    bot, fake = make_engine(env=MAKER)
    setup, sizing, t = _open(bot, fake)
    assert t["status"] == "OPEN" and len(_orders(fake, "LIMIT")) == 1 and not _orders(fake, "MARKET")
    assert _orders(fake, "LIMIT")[0][2]["timeInForce"] == "GTX"
    assert any(a["orderType"] == "STOP_MARKET" for a in fake.algos)
    bot.stop()


def test_maker_partial_fill_is_cancelled_and_protected():
    bot, fake = make_engine(env=MAKER)
    fake.limit_mode = "partial"
    setup, sizing, t = _open(bot, fake)
    assert t["status"] == "OPEN" and 0 < t["qty"] < float(sizing["qty"])
    assert abs(fake.pos[SYMBOL]["qty"]) == pytest.approx(t["qty"])
    assert any(c[0] == "DELETE" and c[1] == "/fapi/v1/order" for c in fake.calls), "the unfilled remainder must be cancelled"
    assert any(a["orderType"] == "STOP_MARKET" for a in fake.algos) and not _orders(fake, "MARKET")
    bot.stop()


@pytest.mark.parametrize("mode", ["rest", "reject"])
def test_maker_falls_back_to_market_or_skips(mode):
    bot, fake = make_engine(env=MAKER)
    fake.limit_mode = mode
    setup, sizing, t = _open(bot, fake)
    assert t["status"] == "OPEN" and len(_orders(fake, "MARKET")) == 1
    assert abs(fake.pos[SYMBOL]["qty"]) == pytest.approx(float(sizing["qty"])), "no double position from limit + market"
    bot.stop()
    bot2, fake2 = make_engine(env={**MAKER, "ENTRY_MAKER_FALLBACK": "skip"})
    fake2.limit_mode = mode
    setup, sizing, t = _open(bot2, fake2)
    assert t["status"] == "FAILED" and not fake2.pos and not _orders(fake2, "MARKET") and not fake2.algos
    bot2.stop()


# ---- trailing stop / funding / liquidation -------------------------------------------------------------
def _stop_trigger(fake):
    stops = [a for a in fake.algos if a["orderType"] == "STOP_MARKET"]
    assert len(stops) == 1, stops
    return float(stops[0]["triggerPrice"])


def test_trailing_stop_follows_price_after_tp1_and_never_retreats():
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    dist = setup.entry - setup.stop                                   # selftest setup: ATR = dist / 2, TP2 = entry + 2.5 dist
    fake.set_price(SYMBOL, setup.tp1 * 1.0005)
    bot.lifecycle.manage()
    be = _stop_trigger(fake)
    assert bot.db.get_trade(t["id"])["meta"]["be_moved"] and be > setup.entry
    fake.set_price(SYMBOL, setup.entry + 2.0 * dist)
    bot.lifecycle.manage()
    trailed = _stop_trigger(fake)
    assert trailed == pytest.approx(setup.entry + 1.0 * dist, rel=1e-3) and trailed > be
    fake.set_price(SYMBOL, setup.entry + 1.6 * dist)                  # pull-back above the stop: the stop must not move down
    bot.lifecycle.manage()
    assert _stop_trigger(fake) == trailed
    fake.set_price(SYMBOL, setup.entry + 2.05 * dist)                 # +0.05 dist = 0.1 ATR: below the 0.25 ATR step
    bot.lifecycle.manage()
    assert _stop_trigger(fake) == trailed and bot.db.get_trade(t["id"])["meta"]["trail_n"] == 1
    fake.set_price(SYMBOL, trailed * 0.999)
    bot.lifecycle.manage()
    done = bot.db.get_trade(t["id"])
    assert done["status"] == "CLOSED" and done["exit_reason"] == "TRAILING_STOP" and done["pnl_usdt"] > 0 and not fake.algos
    bot.stop()


def test_trailing_can_be_disabled():
    bot, fake = make_engine(env={"TRAIL_ATR_MULT": "0"})
    setup, sizing, t = _open(bot, fake)
    dist = setup.entry - setup.stop
    fake.set_price(SYMBOL, setup.tp1 * 1.0005); bot.lifecycle.manage()
    be = _stop_trigger(fake)
    fake.set_price(SYMBOL, setup.entry + 2.0 * dist); bot.lifecycle.manage()
    assert _stop_trigger(fake) == be
    bot.stop()


@pytest.mark.parametrize("lag_ms", [0, 1500])
def test_realised_pnl_includes_entry_fee_and_funding(lag_ms):
    """Regression: the entry fill (and its fee) used to fall outside the PnL window when the exchange timestamp was
    earlier than the local clock after the order returned."""
    fake = FakeBinance()
    fake.fill_lag_ms = lag_ms
    bot, fake = make_engine(fake)
    setup, sizing, t = _open(bot, fake)
    fake.income.append({"symbol": SYMBOL, "incomeType": "FUNDING_FEE", "income": "-0.25", "asset": "USDT", "time": now_ms()})
    fake.income.append({"symbol": "XRPUSDT", "incomeType": "FUNDING_FEE", "income": "-9", "asset": "USDT", "time": now_ms()})
    fake.set_price(SYMBOL, setup.stop * 0.999)
    bot.lifecycle.manage()
    done = bot.db.get_trade(t["id"])
    fills = [r for r in fake.user_trades if r["symbol"] == SYMBOL]
    expected = sum(float(r["realizedPnl"]) - float(r["commission"]) for r in fills) - 0.25      # other symbols' funding is ignored
    assert done["status"] == "CLOSED" and done["pnl_usdt"] == pytest.approx(expected, abs=1e-6)
    assert done["meta"]["funding_usdt"] == -0.25
    bot.stop()
    bot, fake = make_engine()
    setup, sizing, t = _open(bot, fake)
    fake.set_price(SYMBOL, setup.stop * 0.999)
    bot.lifecycle.manage()
    assert bot.db.get_trade(t["id"])["meta"]["funding_usdt"] == 0
    bot.stop()


def test_stop_near_liquidation_is_refused():
    bot, fake = make_engine(env={"LEVERAGE": "5", "MAX_LEVERAGE": "5", "RISK_PER_TRADE_PCT": "1.0"})
    px = fake.price(SYMBOL)
    wide = TradeSetup(SYMBOL, "swing", "LONG", px, px * 0.85, px * 1.18, px * 1.45, px * 0.05, 3.0, 15.0, [])
    res = bot.risk.size(wide, bot.exec.filters(SYMBOL), 100.0, 100.0)
    assert not res["ok"] and "LIQUIDATION" in res["reason"]
    assert bot.risk.size(_setup(fake), bot.exec.filters(SYMBOL), 100.0, 100.0)["ok"]
    bot.stop()


# ---- user stream ---------------------------------------------------------------------------------------
def test_user_stream_only_wakes_the_guardian():
    fake = FakeBinance()
    rest = RestClient("https://fapi.binance.com", fake.api_key, fake.api_secret, transport=fake.transport)
    woke = []
    us = UserStream(rest, "wss://fstream.binance.com", "private/ws", woke.append, idle_ws_factory)
    assert us.start() and us.listen_key == "fake-listen-key-1" and us.conn.url.endswith("/private/ws/fake-listen-key-1")
    us.handle({"e": "ORDER_TRADE_UPDATE", "o": {}}); us.handle({"data": {"e": "ACCOUNT_UPDATE"}}); us.handle({"e": "MARGIN_CALL"})
    assert woke == ["ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"] and us.status()["events"] == 2
    us.stop()
    bad = UserStream(RestClient("https://fapi.binance.com", "WRONG", "x", transport=fake.transport), "wss://x", "ws", woke.append, idle_ws_factory)
    assert bad.start() is False and bad.last_error, "a failing listenKey leaves the stream off instead of raising"


def test_execution_settings_validation():
    s = load_settings({})
    assert s.entry_mode == "market" and not s.user_stream and not s.telegram_enabled and s.trail_atr_mult == 2.0
    assert load_settings(TG).telegram_enabled
    for bad in ({"ENTRY_MODE": "x"}, {"TELEGRAM_SIGNALS": "all"}, {"ENTRY_MAKER_FALLBACK": "wait"}, {"TRAIL_ATR_MULT": "0.2"},
                {"ENTRY_MAKER_WAIT_S": "0"}, {"TELEGRAM_DIGEST_HOURS": "-1"}):
        with pytest.raises(ValueError):
            load_settings(bad)


def test_multi_assets_mode_query_failure_blocks_preflight(monkeypatch):
    bot, fake = make_engine()
    original = bot.exec.rest.request

    def failing_request(method, path, *args, **kwargs):
        if path == "/fapi/v1/multiAssetsMargin":
            raise RuntimeError("simulated account-mode endpoint outage")
        return original(method, path, *args, **kwargs)

    monkeypatch.setattr(bot.exec.rest, "request", failing_request)
    result = bot.exec.preflight()
    assert not result["ok"]
    assert any("multi-assets mode query failed; refusing execution" in e for e in result["errors"])
    bot.stop()


def test_guardian_replaces_wrong_trigger_stop_without_leaving_stale_stop():
    bot, fake = make_engine()
    setup, sizing, trade = _open(bot, fake)
    old = next(a for a in fake.algos if a["orderType"] == "STOP_MARKET")
    old["triggerPrice"] = str(setup.stop * 0.98)
    bot.lifecycle.manage()
    stops = [o for o in bot.exec.protective_orders(SYMBOL) if o.kind == "STOP"]
    assert len(stops) == 1
    assert stops[0].trigger == pytest.approx(setup.stop, rel=1e-4)
    assert stops[0].close_position or stops[0].qty >= abs(fake.pos[SYMBOL]["qty"])
    bot.stop()
