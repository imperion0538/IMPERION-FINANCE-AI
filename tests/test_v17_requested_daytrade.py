"""Requested V17 Day Trade policy acceptance, with mocked market and exchange."""
from dataclasses import replace
from types import SimpleNamespace
import math
import pytest

from imperion.config import Settings, STYLES, load_settings
from imperion.data.admission import RadarAdmission
from imperion.db import Database
from imperion.fakes import make_engine
from imperion.risk.manager import RiskManager
from imperion.util import now_ms
from .helpers import crafted_analyze


def exposure(symbol, side="LONG", risk=1.0):
    return {"symbol": symbol, "side": side, "risk_usdt": risk, "meta": {}, "style": "day"}


def test_requested_defaults_are_actual_runtime_defaults():
    for cfg in (Settings(), load_settings({})):
        assert cfg.active_styles == ("day",)
        assert (cfg.risk_per_trade_pct, cfg.max_open_risk_pct,
                cfg.max_daily_new_risk_pct, cfg.max_concurrent_positions) == (1.0, 10.0, 10.0, 5)
        assert cfg.radar_trading_enabled
        assert cfg.radar_trade_allowlist == ()
        assert cfg.radar_count == cfg.radar_deep_count == cfg.radar_max_active == 10
        assert cfg.symbols == (
            "BTCUSDT", "ETHUSDT", "AVAXUSDT", "INJUSDT", "LINKUSDT", "SOLUSDT",
            "LTCUSDT", "DOTUSDT", "FILUSDT", "XRPUSDT", "SUIUSDT",
        )
        assert cfg.max_risk_overshoot == 1.0
        assert cfg.daily_loss_limit_pct == cfg.max_drawdown_pct == 0
        assert cfg.consecutive_loss_pause == 0
        assert not cfg.orders_possible


@pytest.mark.parametrize("env", [
    {"ACTIVE_STYLES":"scalp"}, {"ACTIVE_STYLES":"scalp,day"},
    {"RISK_PER_TRADE_PCT":"1.01"}, {"MAX_OPEN_RISK_PCT":"10.01"},
    {"MAX_DAILY_NEW_RISK_PCT":"10.01"}, {"MAX_CONCURRENT_POSITIONS":"6"},
    {"MAX_RISK_OVERSHOOT":"1.25"}, {"MAX_POSITIONS":"not-applicable",
     "RISK_PER_TRADE_PCT":"nan"},
])
def test_unsafe_config_is_rejected(env):
    with pytest.raises(ValueError):
        load_settings(env)


def test_five_positions_allowed_sixth_blocked_even_all_long():
    manager = RiskManager(load_settings({}), Database(":memory:"))
    day = STYLES["day"]
    others = [exposure(s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")]
    accepted = manager.precheck("ADAUSDT", day, "LONG", others, 100.0)
    assert accepted["allowed"], accepted
    refused = manager.precheck("XRPUSDT", day, "LONG", others+[exposure("ADAUSDT")], 100.0)
    assert any("MAX_POSITIONS" in x for x in refused["reasons"])


def test_each_risk_budget_is_separate_and_cost_sensitive():
    db = Database(":memory:")
    manager = RiskManager(load_settings({}), db)
    day = STYLES["day"]
    assert manager.precheck("ADAUSDT", day, "LONG", [exposure("BTCUSDT", risk=8.5)], 100.0)["allowed"]
    too_much = manager.precheck("ADAUSDT", day, "LONG", [exposure("BTCUSDT", risk=9.5)], 100.0)
    assert any("OPEN_RISK_LIMIT" in x for x in too_much["reasons"])
    db.kv_set("risk.daily_new_risk", 0.095)
    daily = manager.precheck("ADAUSDT", day, "LONG", [], 100.0)
    assert any("DAILY_NEW_RISK_LIMIT" in x for x in daily["reasons"])
    db.kv_set("risk.daily_new_risk", 0.0)
    huge = manager.precheck("ADAUSDT", day, "LONG", [], 100.0, new_risk_usdt=1.0001)
    assert any("PER_TRADE_RISK_LIMIT" in x for x in huge["reasons"])


def test_manual_and_exchange_incident_stops_remain_active():
    cfg = load_settings({})
    risk = RiskManager(cfg, Database(":memory:"))
    risk.update_equity(100.0)
    risk.update_equity(80.0)  # optional loss/drawdown policy disabled
    assert not risk.halted()
    risk.halt("UNKNOWN_EXCHANGE_ORDER", "lifecycle")
    assert not risk.precheck("ADAUSDT", STYLES["day"], "LONG", [], 100.0)["allowed"]


def test_dynamic_radar_a_and_a_plus_without_manual_allowlist_and_b_denied():
    cfg = load_settings({"RADAR_MIN_QUOTE_VOLUME":"1"})
    radar = RadarAdmission(cfg)
    now = now_ms()
    rows = [{"symbol":s, "eligible":True, "score":0.8, "quote_volume":10000000}
            for s in ("HYPEUSDT", "BNBUSDT", "ADAUSDT")]
    deep = {s:{"ts":now, "approved":True, "grade":g, "direction":"LONG", "setup":{"entry":100}}
            for s,g in (("HYPEUSDT","A+"),("BNBUSDT","A"),("ADAUSDT","B"))}
    filters={s:SimpleNamespace(status="TRADING",contract_type="PERPETUAL") for s in deep}
    radar.replace(rows,deep,radar_updated=now,filters=filters,filters_updated=now,now=now,healthy=lambda s:True)
    assert radar.symbols(now) == ["HYPEUSDT", "BNBUSDT"]
    assert radar.snapshot(now)["rejected"]["ADAUSDT"] == "DEEP_ANALYSIS_NOT_APPROVED"


def test_engine_blocks_b_even_if_director_mock_approves():
    engine, fake = make_engine(env={"RADAR_TRADING_ENABLED":"false"})
    try:
        engine.director.analyze=crafted_analyze(fake, grade="B", quality=73.0)
        r=engine.evaluate("SOLUSDT","day")
        assert not r["traded"]
        assert not fake.pos
        assert "QUALITY_A_PLUS_OR_A_REQUIRED" in r["reasons"]
    finally:
        engine.stop()


def test_engine_rejects_non_day_even_for_fixed_coin():
    engine, fake = make_engine(env={"RADAR_TRADING_ENABLED":"false"})
    try:
        with pytest.raises(ValueError,match="DAY_TRADE_ONLY"):
            engine.evaluate("BTCUSDT","scalp")
    finally:
        engine.stop()


def test_decision_pass_prioritizes_a_plus_across_fixed_symbols(monkeypatch):
    import imperion.runtime.engine as runtime_engine
    engine, fake = make_engine(env={"RADAR_TRADING_ENABLED":"false"})
    seen=[]
    try:
        def analysis(symbol,style,*args,**kwargs):
            grade = "A+" if symbol == "SUIUSDT" else "A"
            return crafted_analyze(fake,grade=grade,quality=92.0 if grade=="A+" else 80.0)(symbol,style)
        monkeypatch.setattr(engine.director,"analyze",analysis)
        monkeypatch.setattr(runtime_engine,"last_closed_open",lambda *args: -1)
        monkeypatch.setattr(engine,"evaluate",lambda symbol,style,**kwargs:seen.append((symbol,kwargs.get("prepared").grade)))
        assert engine.decision_pass()==len(engine.cfg.symbols)
        assert len(seen)==len(engine.cfg.symbols)
        assert seen[0] == ("SUIUSDT","A+")
        assert all(g == "A" for _,g in seen[1:])
        assert engine.decision_pass()==0
    finally:
        engine.stop()
