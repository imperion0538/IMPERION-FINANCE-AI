"""Tiered radar: tier-1 scoring of every candidate, tier-2 Director analysis of the best few, never traded."""
import pytest

from imperion.analysis.radar import ATR_BAND, select_deep, tier1
from imperion.fakes import make_engine
from imperion.models import AgentReport

from .helpers import crafted_analyze, make_candles, trend


class Store:
    def __init__(self, data): self.data = data
    def get(self, sym, tf, n=None):
        cs = self.data.get((sym, tf), [])
        return cs[-n:] if n else cs


def test_tier1_scores_filters_and_ranks():
    good = make_candles(trend(200, drift=0.002, amp=0.004), step=3_600_000)
    flat = make_candles([100.0] * 200, wick=0.0001, step=3_600_000)                # dead market: below the volatility band
    short = make_candles(trend(50, drift=0.002, amp=0.004), step=3_600_000)
    store = Store({("AAAUSDT", "1h"): good, ("BBBUSDT", "1h"): flat, ("CCCUSDT", "1h"): short, ("DDDUSDT", "1h"): good})
    cands = [{"symbol": "AAAUSDT", "quote_volume": 9e8, "price_change_pct": 6.0}, {"symbol": "BBBUSDT", "quote_volume": 8e8, "price_change_pct": 0.1},
             {"symbol": "CCCUSDT", "quote_volume": 7e8, "price_change_pct": 3.0}, {"symbol": "DDDUSDT", "quote_volume": 1e8, "price_change_pct": 1.0}]
    ranked = tier1(cands, store)
    assert [r["symbol"] for r in ranked if r["eligible"]] == ["AAAUSDT", "DDDUSDT"], ranked
    bad = {r["symbol"]: r["why_not"] for r in ranked if not r["eligible"]}
    assert any("volatility" in w for w in bad["BBBUSDT"]) and any("history" in w for w in bad["CCCUSDT"])
    assert select_deep(ranked, 1) == ["AAAUSDT"] and select_deep(ranked, 0) == []
    assert ATR_BAND[0] < ATR_BAND[1]


def test_radar_scan_deepens_the_best_candidates_and_never_trades(monkeypatch):
    engine, fake = make_engine(env={"RADAR_DEEP_COUNT": "2", "RADAR_TRADING_ENABLED": "false"})
    engine._refresh_radar()
    assert set(engine.radar.symbols()) == {"HYPEUSDT", "INJUSDT", "FILUSDT"}
    real = engine.director.analyze
    approved = crafted_analyze(fake)
    monkeypatch.setattr(engine.director, "analyze", lambda sym, style, *a, **k: approved(sym, style) if sym in engine.radar.symbols() else real(sym, style, *a, **k))
    out = engine.radar_scan()
    assert len(out["ranked"]) == 3 and 1 <= len(out["deep"]) <= 2, out["ranked"]
    for sym, d in out["deep"].items():
        assert sym not in engine.cfg.symbols and d["approved"] and d["setup"]
        assert all(engine.candles.count(sym, tf) >= 150 for tf in ("5m", "15m", "1h", "4h"))
    assert not fake.pos and not engine.db.query("SELECT * FROM trades"), "radar symbols are never traded"
    with pytest.raises(ValueError):
        engine.evaluate(next(iter(out["deep"])), "day")
    assert engine.status()["radar_tiers"]["ranked"]
    assert isinstance(AgentReport("x").to_dict(), dict)
    engine.stop()


def test_scan_signals_are_marked_scan_only(monkeypatch):
    from .test_telegram import TG, FakeTelegram
    cap = FakeTelegram()
    engine, fake = make_engine(env={**TG, "RADAR_DEEP_COUNT": "3", "RADAR_TRADING_ENABLED": "false"}, telegram_transport=cap)
    engine.tg.min_interval = 0.0
    engine.tg.flush(); cap.sent.clear()
    engine._refresh_radar()
    approved = crafted_analyze(fake)
    monkeypatch.setattr(engine.director, "analyze", lambda sym, style, *a, **k: approved(sym, style))
    out = engine.radar_scan()
    engine.tg.flush()
    assert out["deep"] and cap.sent and all("TARAMA LİSTESİ" in m for m in cap.sent)
    n = len(cap.sent)
    engine.radar_scan(); engine.tg.flush()
    assert len(cap.sent) == n, "scan signals have their own cooldown"
    engine.stop()
