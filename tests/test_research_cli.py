"""Backtester (real Director, no look-ahead), CLI analyze/backtest, dashboard page, market analysis helpers."""
import argparse
import json
import threading
import urllib.request

import pytest

from imperion import cli
from imperion.analysis import market, orderflow
from imperion.analysis.indicators import adx, atr, ema, macd_hist, percentile_rank, rsi
from imperion.analysis.mtf import alignment, mtf_view, tf_bias
from imperion.config import TIMEFRAMES, load_settings
from imperion.data.microstructure import BookStore, TradeStore
from imperion.exchange.rest import RestClient
from imperion.fakes import FakeBinance, make_engine
from imperion.models import BEARISH, BULLISH, NEUTRAL
from imperion.research import backtest as bt
from imperion.server.http import make_server
from imperion.util import now_ms

from .helpers import make_candles, random_walk, trend


# ---- indicators / market state -------------------------------------------------------------------------
def test_indicators():
    up, down = [100 * 1.01 ** i for i in range(80)], [100 * 0.99 ** i for i in range(80)]
    assert rsi(up) == 100.0 and rsi(down) < 1 and rsi([1.0] * 5) is None
    assert macd_hist(up)[-1] != 0 and macd_hist(up[:20]) == []
    a = adx(make_candles(up, wick=0.001))
    assert a is not None and a[0] > 40 and a[1] > a[2]
    assert percentile_rank(5, [1, 2, 3, 4, 10]) == 0.8 and percentile_rank(1, []) == 0.5
    assert atr(make_candles([100.0] * 40)) > 0 and ema([1, 2, 3, 4, 5], 3) is not None and ema([1.0], 3) is None


def test_market_state_readings():
    up = make_candles(trend(320, drift=0.004, amp=0.001))
    assert market.regime(up, "UP")["regime"] == "TREND_UP"
    assert market.regime(make_candles(trend(320, drift=-0.004, amp=0.001)), "DOWN")["regime"] == "TREND_DOWN"
    assert market.regime(up[:50])["regime"] == "UNKNOWN"
    assert market.volatility_state(make_candles(random_walk(320, 1)))["state"] in ("SQUEEZE", "NORMAL", "EXPANSION")
    m = market.momentum(make_candles([100 * 1.02 ** i for i in range(120)]))
    assert m["available"] and m["exhausted"] == "UP"
    day = 1_700_006_400_000 - 1_700_006_400_000 % 86_400_000
    assert [market.session(day + h * 3_600_000) for h in (3, 9, 13, 18, 22)] == ["ASIA", "LONDON", "LONDON_NY_OVERLAP", "NEW_YORK", "OFF_HOURS"]


def test_breadth_and_mtf():
    class Store:
        def __init__(self, data): self.data = data
        def get(self, sym, tf, n=None): return self.data.get((sym, tf), [])[-n:] if n else self.data.get((sym, tf), [])
    rising = make_candles(trend(320, drift=0.003, amp=0.002))
    falling = make_candles(trend(320, drift=-0.003, amp=0.002))
    syms = [f"S{i}USDT" for i in range(6)]
    assert market.breadth(Store({(s, "1h"): rising for s in syms}), syms, "1h")["direction"] == BULLISH
    assert market.breadth(Store({(s, "1h"): falling for s in syms}), syms, "1h")["direction"] == BEARISH
    assert not market.breadth(Store({}), syms, "1h")["available"]
    store = Store({("X", tf): rising for tf in TIMEFRAMES})
    view = mtf_view(store, "X", TIMEFRAMES)
    assert view["direction"] == BULLISH and alignment(view, BULLISH) == 1.0 and alignment(view, BEARISH) == 0.0
    assert tf_bias(rising[:20])["bias"] == "UNKNOWN"


def test_order_flow_readings():
    closes = trend(120, drift=0.001)
    assert orderflow.candle_flow(make_candles(closes, buy_frac=0.8))["score"] > 0.15
    assert orderflow.candle_flow(make_candles(closes, buy_frac=0.2))["score"] < -0.15
    assert not orderflow.candle_flow(make_candles(closes[:30]))["available"]
    trades, books, now = TradeStore(50_000.0), BookStore(), now_ms()
    assert not orderflow.live_flow(trades, books, "SOLUSDT", now)["available"]
    books.update("SOLUSDT", [[f"{100 - i * 0.1:.1f}", "50"] for i in range(20)], [[f"{100.1 + i * 0.1:.1f}", "5"] for i in range(20)], now)
    f = orderflow.live_flow(trades, books, "SOLUSDT", now)
    assert f["available"] and f["book_imbalance"] > 0.5 and f["score"] > 0
    assert not orderflow.live_flow(trades, books, "SOLUSDT", now + 60_000)["available"], "stale books are not used"
    # The live-flow profile must expose aggTrade-derived CVD, separately from candle delta.
    live_trades = TradeStore(whale_floor_usdt=1_000_000)
    base = now - now % 60_000
    for minute in range(5):
        for j, price in enumerate((100.01, 100.05, 100.09, 100.13)):
            ts = base - 4 * 60_000 + minute * 60_000 + j * 1_000
            live_trades.add("SOLUSDT", ts, price, 10.0, False)
            live_trades.add("SOLUSDT", ts + 100, price, 1.0, True)
    lf = orderflow.live_flow(live_trades, books, "SOLUSDT", now)
    assert lf["available"] and lf["trade_flow"]["cvd"] > 0 and lf["trade_flow"]["delta_ratio"] > 0
    assert "footprint" in lf and lf["footprint"]["levels"] > 0


# ---- backtester ----------------------------------------------------------------------------------------
def test_replay_store_never_looks_ahead():
    cs = make_candles([100.0 + i for i in range(50)])
    store = bt.ReplayStore({("SOLUSDT", "1m"): cs}, "1m")
    store.cursor = cs[19].ct + 1
    seen = store.get("SOLUSDT", "1m")
    assert len(seen) == 20 and seen[-1].t == cs[19].t and store.count("SOLUSDT", "1m") == 20
    assert store.last_price("SOLUSDT") == cs[19].c and store.get("ETHUSDT", "1m") == [] and len(store.get("SOLUSDT", "1m", 5)) == 5


def test_backtest_runs_the_real_director_on_history(capsys):
    fake = FakeBinance()
    rest = RestClient("https://fapi.binance.com", transport=fake.transport)
    end = now_ms()
    start = end - 8 * 3_600_000
    series = bt.download(rest, "SOLUSDT", "scalp", start, end)
    assert {tf for s, tf in series if s == "SOLUSDT"} == set(TIMEFRAMES) and ("BTCUSDT", "15m") in series
    assert all(c.ct < end for cs in series.values() for c in cs)
    assert bt.replay_timeframes("swing") == ("1h", "4h", "1d", "1w")
    rep = bt.run_backtest(series, "SOLUSDT", "scalp", load_settings({}), start_ms=start, step=6)
    f = rep["funnel"]
    assert f["evaluated"] > 40 and f["evaluated"] >= f["playbook"] and f["planned"] >= f["approved"] >= f["traded"]
    assert rep["summary"]["trades"] == f["traded"] == len(rep["trades"]) and isinstance(rep["rejections"], dict)
    assert "BACKTEST SOLUSDT" in bt.format_report(rep)
    assert cli.cmd_backtest(argparse.Namespace(symbol="solusdt", style="scalp", days=0.2, step=12, json=True), rest=rest) == 0
    assert json.loads(capsys.readouterr().out)["symbol"] == "SOLUSDT"
    with pytest.raises(ValueError):
        cli.cmd_backtest(argparse.Namespace(symbol="SOLUSDT", style="nope", days=1, step=1, json=False), rest=rest)


def test_backtest_statistics():
    s = bt._stats([{"r": 1.5, "status": "TARGET", "playbook": "a", "grade": "A", "regime": "TREND_UP", "side": "LONG"},
                   {"r": -1.1, "status": "STOP", "playbook": "b", "grade": "B", "regime": "RANGE", "side": "LONG"},
                   {"r": -1.1, "status": "STOP", "playbook": "b", "grade": "B", "regime": "RANGE", "side": "SHORT"},
                   {"r": 0.6, "status": "BE_AFTER_TP1", "playbook": "a", "grade": "A", "regime": "TREND_UP", "side": "LONG"}])
    assert s["trades"] == 4 and s["win_rate"] == 0.5 and s["total_r"] == pytest.approx(-0.1) and s["max_drawdown_r"] == pytest.approx(2.2)
    assert s["by"]["playbook"]["b"]["n"] == 2 and s["by"]["grade"]["A"]["expectancy_r"] == pytest.approx(1.05)
    assert bt._stats([])["trades"] == 0


# ---- CLI -----------------------------------------------------------------------------------------------
def test_cli_analyze_is_read_only_and_explains_itself(capsys):
    fake = FakeBinance()
    rest = RestClient("https://fapi.binance.com", transport=fake.transport)
    res = cli.analyze_symbol(load_settings({}), "SOLUSDT", ["scalp", "day"], rest=rest)
    assert [r["style"] for r in res] == ["scalp", "day"]
    for r in res:
        assert r["direction"] in ("LONG", "SHORT", "NO TRADE") and r["agents"] and r["reasons"] and r["maps"]["setup"]["ok"]
    cli._print_analysis(res[0])
    out = capsys.readouterr().out
    assert "SOLUSDT / scalp" in out and "trend" in out
    assert not [c for c in fake.calls if c[0] != "GET"], "analysis never writes to the exchange"
    assert cli.main(["analyze", "--symbol", "SOLUSDT", "--style", "yolo"]) == 2


# ---- dashboard -----------------------------------------------------------------------------------------
def test_dashboard_page_is_public_static_and_secret_free():
    engine, fake = make_engine()
    srv = make_server(engine.cfg, engine.db, engine, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/dashboard", timeout=5) as r:
            body = r.read().decode()
            assert r.status == 200 and "text/html" in r.headers["Content-Type"] and "Content-Security-Policy" in r.headers
        assert "IMPERION" in body and "/status" in body and "Kurulum" in body
        for secret in (engine.cfg.admin_token, engine.cfg.webhook_secret, fake.api_key, fake.api_secret):
            assert secret not in body
        assert "innerHTML" not in body, "values must be written with textContent"
    finally:
        srv.shutdown(); srv.server_close(); engine.stop()


def test_settings_universe_rules():
    """V17 fixed 11 + dynamic radar 10; reject invalid universe config."""
    s = load_settings({})
    assert len(s.symbols) == 11 and s.radar_count == 10 and s.mode == "dry" and not s.orders_possible
    assert len(load_settings({"SYMBOLS": "BTCUSDT,ETHUSDT"}).symbols) == 2, "the execution universe is configurable"
    for bad in ({"SYMBOLS": "BTCUSDT,BTCUSDT"}, {"SYMBOLS": ",".join(f"S{i}USDT" for i in range(65))}, {"RADAR_COUNT": "31"},
                {"TELEGRAM_MIN_GRADE": "C"}, {"ACTIVE_STYLES": ""}, {"SYMBOLS": "BTC-USD"}):
        with pytest.raises(ValueError):
            load_settings(bad)
    assert NEUTRAL == "NEUTRAL"


def test_timeframe_cache_never_mixes_data_sets():
    class Store:
        def __init__(self, cs): self.cs = cs
        def get(self, sym, tf, n=None): return self.cs
    up = make_candles(trend(320, drift=0.003, amp=0.002))
    down = make_candles(trend(320, drift=-0.003, amp=0.002))       # same symbol, same timestamps, different prices
    assert mtf_view(Store(up), "SOLUSDT", ("1h",))["direction"] == BULLISH
    assert mtf_view(Store(down), "SOLUSDT", ("1h",))["direction"] == BEARISH


def test_sensitivity_replays_every_threshold_variant(capsys):
    from imperion.research.sensitivity import STEPS, format_sensitivity, run_sensitivity, variants
    fake = FakeBinance()
    rest = RestClient("https://fapi.binance.com", transport=fake.transport)
    end = now_ms()
    start = end - 4 * 3_600_000
    series = bt.download(rest, "SOLUSDT", "scalp", start, end)
    rep = run_sensitivity(series, "SOLUSDT", "scalp", load_settings({}), start_ms=start, step=10)
    assert len(rep["rows"]) == len(variants()) == 1 + 2 * len(STEPS) and rep["verdict"] in ("STABLE", "FRAGILE", "insufficient trades")
    assert rep["rows"][0]["variant"] == "baseline" and "SENSITIVITY SOLUSDT" in format_sensitivity(rep)
    args = argparse.Namespace(symbol="SOLUSDT", style="scalp", days=0.1, step=20, json=True)
    assert cli.cmd_sensitivity(args, rest=rest) == 0 and json.loads(capsys.readouterr().out)["rows"]


def test_trade_store_aggtrade_cvd_and_footprint_profile():
    trades = TradeStore(whale_floor_usdt=1_000_000)
    base = 1_700_000_000_000 - 1_700_000_000_000 % 60_000
    # Binance buyer-is-maker=False means aggressive buyer; True means aggressive seller.
    for minute in range(5):
        for j, price in enumerate((100.01, 100.05, 100.09, 100.13)):
            ts = base + minute * 60_000 + j * 1_000
            trades.add("BTCUSDT", ts, price, 10.0, False)
            trades.add("BTCUSDT", ts + 100, price, 1.0, True)
    profile = trades.flow_profile("BTCUSDT", 5, now=base + 4 * 60_000 + 30_000)
    assert profile["available"] and profile["cvd"] > 0 and profile["delta_ratio"] > 0.7
    assert profile["volume_qty"] == pytest.approx(220.0)
    assert profile["levels"] and profile["poc_bin"] is not None
    stacks = trades.stacked_imbalances("BTCUSDT", 5, now=base + 4 * 60_000 + 30_000)
    assert stacks["buy_stacks"] >= 1


def test_market_validation_report_has_out_of_sample_forward_metrics():
    from scripts.market_validation import evaluate
    primary = make_candles(trend(150, drift=0.001, amp=0.01))
    comparison = make_candles(trend(150, drift=0.0008, amp=0.012))
    report = evaluate(primary, horizon=5, comparison=comparison)
    assert report["candles"] == 150 and report["horizon_bars"] == 5
    assert "gross returns exclude fees" in report["method"]
    assert isinstance(report["signals"], dict)
