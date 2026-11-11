import time

import pytest

from imperion.data.candles import CandlePoller, CandleStore
from imperion.data.health import HealthMonitor
from imperion.data.microstructure import BookStore, TradeStore
from imperion.exchange.rest import BinanceAPIError, NetworkError, RateLimited, RestClient
from imperion.exchange.ws import MarketStreams, StreamConnection
from imperion.fakes import FakeBinance, FakeWSFactory, mark_streams_live, warm_feed
from imperion.util import last_closed_open, now_ms

TF = ("1m", "5m", "15m")


def client(fake=None, key="TESTKEY", secret="TESTSECRET"):
    fake = fake or FakeBinance()
    return RestClient("https://fapi.binance.com", key, secret, transport=fake.transport), fake


# ------------------------------------------------------------------ REST
def test_signed_requests_validate_against_strict_fake():
    c, fake = client()
    assert c.request("GET", "/fapi/v3/account", signed=True)["availableBalance"]
    bad, _ = client(fake, secret="WRONG")
    with pytest.raises(BinanceAPIError) as e:
        bad.request("GET", "/fapi/v3/account", signed=True)
    assert e.value.code == -1022
    nokey = RestClient("https://fapi.binance.com", transport=fake.transport)
    with pytest.raises(BinanceAPIError):
        nokey.request("GET", "/fapi/v3/account", signed=True)


def test_clock_skew_triggers_resync_then_succeeds():
    c, fake = client()
    fake.time_skew_ms = 20_000                # server clock is 20 s ahead of ours
    assert c.request("GET", "/fapi/v3/account", signed=True)         # -1021 -> sync_time -> retry
    assert abs(c.time_offset_ms - 20_000) < 500


def test_rate_limit_handling():
    def t429(method, url, headers, timeout):
        return 429, {"retry-after": "7"}, "{}"
    rc = RestClient("https://x", transport=t429)
    with pytest.raises(RateLimited):
        rc.request("GET", "/fapi/v1/time")
    with pytest.raises(RateLimited):                                  # now in cooldown: non-priority calls are refused locally
        rc.request("GET", "/fapi/v1/klines")
    assert rc.guard.cooldown_until > time.time() + 5

    def t418(method, url, headers, timeout):
        return 418, {"retry-after": "300"}, "{}"
    rb = RestClient("https://x", transport=t418)
    with pytest.raises(RateLimited):
        rb.request("GET", "/a")
    with pytest.raises(RateLimited):
        rb.request("GET", "/a", priority=True)                        # a ban blocks even priority calls


def test_network_errors_retry_get_but_not_post():
    calls = {"n": 0}

    def flaky(method, url, headers, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise NetworkError("timeout")
        return 200, {}, '{"ok": true}'
    rc = RestClient("https://x", transport=flaky)
    assert rc.request("GET", "/x") == {"ok": True} and calls["n"] == 3
    calls["n"] = 0
    with pytest.raises(NetworkError):
        rc.request("POST", "/x", signed=False)
    assert calls["n"] == 1


def test_region_block_is_reported_clearly():
    rc = RestClient("https://x", transport=lambda *a: (451, {}, "<html>blocked</html>".replace("<", "{").replace(">", "}")))
    with pytest.raises(BinanceAPIError) as e:
        rc.request("GET", "/x")
    assert e.value.code in (-451, -1)


def test_legacy_endpoint_rejects_conditional_orders():
    c, fake = client()
    with pytest.raises(BinanceAPIError) as e:
        c.request("POST", "/fapi/v1/order", {"symbol": "SOLUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "100", "closePosition": "true"}, signed=True)
    assert e.value.code == -4120


# ------------------------------------------------------------------ candles
def test_candle_bootstrap_and_poll():
    c, fake = client()
    store = CandleStore()
    poller = CandlePoller(c, store, ["SOLUSDT"], TF)
    assert poller.bootstrap(limit=300) == 3
    for tf in TF:
        assert store.count("SOLUSDT", tf) >= 250
        assert not store.has_gap("SOLUSDT", tf)
        got = store.get("SOLUSDT", tf)
        assert all(x.ct < now_ms() for x in got), "only closed candles are exposed"
    first = store.get("SOLUSDT", "1m")[-1]
    assert first.t == last_closed_open(now_ms(), "1m") or first.t == last_closed_open(now_ms(), "1m") - 60_000


def test_candle_store_rejects_forming_and_dedupes():
    c, fake = client()
    store = CandleStore()
    rows = c.klines("SOLUSDT", "5m", limit=50)
    n = store.ingest("SOLUSDT", "5m", rows)
    assert n == store.count("SOLUSDT", "5m")
    assert store.ingest("SOLUSDT", "5m", rows) == 0
    assert store.forming("SOLUSDT", "5m") is not None


# ------------------------------------------------------------------ streams + microstructure
def test_stream_handler_routes_trades_and_depth():
    trades, books = TradeStore(50_000.0), BookStore()
    ms = MarketStreams("wss://x", trades, books, factory=lambda *a, **k: None)
    now = now_ms()
    ms.handle({"stream": "solusdt@aggTrade", "data": {"e": "aggTrade", "s": "SOLUSDT", "T": now, "p": "140.5", "q": "10", "m": False}})
    ms.handle({"stream": "solusdt@depth20@500ms", "data": {"e": "depthUpdate", "s": "SOLUSDT", "E": now, "b": [["140.4", "5"]], "a": [["140.6", "5"]]}})
    ms.handle({"garbage": True})
    ms.handle({"data": "not-a-dict"})
    assert trades.last_price("SOLUSDT") == 140.5 and books.get("SOLUSDT") is not None
    assert trades.last_ts("SOLUSDT") >= now


def test_aggressor_side_semantics():
    trades, now = TradeStore(1_000.0), now_ms()
    trades.add("X", now, 10.0, 1000.0, False)        # buyer is NOT maker => aggressive buy
    trades.add("X", now, 10.0, 400.0, True)          # buyer is maker => aggressive sell
    s = trades.whale_stats("X", 60_000, now)
    assert s["buy_usdt"] > s["sell_usdt"] > 0


def test_fake_websocket_integration_via_real_connection_code():
    fake = FakeBinance()
    trades, books = TradeStore(50_000.0), BookStore()
    streams = MarketStreams("wss://fstream.binance.com", trades, books, factory=FakeWSFactory(fake, interval=0.05))
    streams.start(["SOLUSDT", "BTCUSDT"])
    try:
        deadline = time.time() + 6
        while time.time() < deadline and not (streams.healthy() and books.get("SOLUSDT") and trades.last_ts("BTCUSDT")):
            time.sleep(0.1)
        assert streams.healthy(), streams.status()
        assert books.get("SOLUSDT") and trades.last_ts("BTCUSDT") > 0
        streams.set_symbols(["SOLUSDT", "BTCUSDT", "ETHUSDT"])
        time.sleep(1.0)
        assert trades.last_ts("ETHUSDT") > 0, "newly added (radar) symbol must start streaming"
    finally:
        streams.stop()


def test_websocket_package_missing_is_fail_closed(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "websocket":
            raise ImportError("no websocket")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    conn = StreamConnection("t", "wss://x", lambda p: None)
    conn.start()
    assert conn.available is False
    ms = MarketStreams("wss://x", TradeStore(), BookStore())
    ms.start(["SOLUSDT"])
    assert ms.available is False and ms.healthy() is False


# ------------------------------------------------------------------ health gate
def test_health_gate_fails_closed_and_passes_when_complete():
    c, fake = client()
    store, trades, books = CandleStore(), TradeStore(50_000.0), BookStore()
    CandlePoller(c, store, ["SOLUSDT"], TF).bootstrap(limit=300)
    streams = MarketStreams("wss://x", trades, books, factory=lambda *a, **k: None)
    mon = HealthMonitor(store, trades, books, streams, 0.06, True)
    r = mon.symbol_health("SOLUSDT", TF)
    assert not r.passed and "HEALTH_WS_CONNECTED_FAILED" in r.reasons and "HEALTH_TRADES_FRESH_FAILED" in r.reasons
    warm_feed(fake, trades, books, ["SOLUSDT"], minutes=10)
    mark_streams_live(streams)
    r = mon.symbol_health("SOLUSDT", TF)
    assert r.passed, r.reasons
    # stale candles
    assert not mon.symbol_health("SOLUSDT", TF, now=now_ms() + 3 * 3600_000).passed
    # wide spread
    books.update("SOLUSDT", [["100", "1"]], [["101", "1"]], now_ms())
    assert not mon.symbol_health("SOLUSDT", TF).passed
    # unknown symbol / no history
    assert not mon.symbol_health("NOPEUSDT", TF).passed
    # without required streams (dry mode) candle checks alone decide
    dry = HealthMonitor(store, trades, books, None, 0.06, False)
    assert dry.symbol_health("SOLUSDT", TF).passed
