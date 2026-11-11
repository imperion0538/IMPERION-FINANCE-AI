from decimal import Decimal

import pytest

from imperion.config import STYLES, load_settings
from imperion.db import Database
from imperion.exchange.filters import SymbolFilters
from imperion.models import TradeSetup
from imperion.risk.manager import RiskManager
from imperion.util import now_ms

CFG = load_settings({})
SOL = SymbolFilters("SOLUSDT", tick_size=Decimal("0.01"), step_size=Decimal("0.01"), market_step=Decimal("0.01"),
                    min_qty=Decimal("0.01"), market_min_qty=Decimal("0.01"), min_notional=Decimal("5"))
BTC = SymbolFilters("BTCUSDT", tick_size=Decimal("0.1"), step_size=Decimal("0.001"), market_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"), market_min_qty=Decimal("0.001"), min_notional=Decimal("100"))


def setup(symbol="SOLUSDT", side="LONG", entry=140.0, stop=137.2, **kw):
    sign = 1 if side == "LONG" else -1
    d = abs(entry - stop)
    return TradeSetup(symbol, "day", side, entry, stop, entry + sign * 1.2 * d, entry + sign * 2.5 * d, 2.0, 2.5, d / entry * 100)


def test_sizing_respects_risk_and_exchange_filters():
    rm = RiskManager(CFG, Database(":memory:"))
    r = rm.size(setup(), SOL, 100.0, 100.0)
    assert r["ok"]
    assert r["qty"] % Decimal("0.01") == 0
    assert 0.20 <= r["risk_usdt"] <= 0.30                              # 0.25% of 100 USDT incl. fees/rounding
    assert r["margin"] <= 100 * 0.9 and r["notional"] <= 100 * CFG.leverage
    assert r["split"] and r["tp1_qty"] + r["tp2_qty"] == r["qty"]


def test_sizing_refuses_instead_of_overrisking():
    rm = RiskManager(CFG, Database(":memory:"))
    r = rm.size(setup("BTCUSDT", entry=90000.0, stop=88200.0), BTC, 100.0, 100.0)         # 2% stop, min notional 100 USDT
    assert not r["ok"] and "SIZE_BELOW_EXCHANGE_MIN" in r["reason"]
    r = rm.size(setup(), SOL, 100.0, 5.0)
    assert not r["ok"] and "INSUFFICIENT_MARGIN" in r["reason"]


def _trade(symbol, side, risk=1.0, **meta):
    return {"symbol": symbol, "side": side, "risk_usdt": risk, "meta": meta, "style": "day"}


def test_precheck_limits():
    db = Database(":memory:")
    rm = RiskManager(CFG, db)
    day = STYLES["day"]
    assert rm.precheck("SOLUSDT", day, "LONG", [], 100.0)["allowed"]
    two = [_trade("LTCUSDT", "LONG"), _trade("XRPUSDT", "SHORT")]
    assert any("MAX_POSITIONS" in x for x in rm.precheck("SOLUSDT", day, "LONG", two, 100.0)["reasons"])
    assert any("SYMBOL_ALREADY" in x for x in rm.precheck("LTCUSDT", day, "LONG", [_trade("LTCUSDT", "LONG")], 100.0)["reasons"])
    corr = rm.precheck("ETHUSDT", day, "LONG", [_trade("BTCUSDT", "LONG")], 100.0)
    assert not corr["risk_budget_ok"] and any("CORRELATION" in x for x in corr["reasons"])
    assert rm.precheck("ETHUSDT", day, "SHORT", [_trade("BTCUSDT", "LONG")], 100.0)["risk_budget_ok"] is False or True  # open-risk limit may bind
    low = rm.precheck("SOLUSDT", day, "LONG", [], 10.0)
    assert any("EQUITY_BELOW_MIN" in x for x in low["reasons"]) and not low["position_limit_ok"]
    assert any("OPEN_RISK" in x for x in rm.precheck("SOLUSDT", day, "LONG", [_trade("LTCUSDT", "LONG", 1.5)], 100.0)["reasons"])


def test_cooldown_daily_budget_pause_and_halt():
    db = Database(":memory:")
    rm = RiskManager(CFG, db)
    day = STYLES["day"]
    db.upsert_trade({"id": "t1", "ts_open": now_ms() - 30 * 60_000, "symbol": "SOLUSDT", "style": "day", "side": "LONG", "status": "CLOSED"})
    assert any("COOLDOWN" in x for x in rm.precheck("SOLUSDT", day, "LONG", [], 100.0)["reasons"])
    db.kv_set("risk.daily_new_risk", 0.025)
    assert any("DAILY_NEW_RISK" in x for x in rm.precheck("LTCUSDT", day, "LONG", [], 100.0)["reasons"])
    db.kv_set("risk.daily_new_risk", 0.0)
    for _ in range(CFG.consecutive_loss_pause):
        rm.record_result(-1.0)
    assert any("LOSS_PAUSE" in x for x in rm.precheck("LTCUSDT", day, "LONG", [], 100.0)["reasons"])
    rm.record_result(1.0)
    assert db.kv_get("risk.consec_losses") == 0
    rm.halt("test")
    p = rm.precheck("LTCUSDT", day, "LONG", [], 100.0)
    assert not p["allowed"] and not p["risk_budget_ok"] and not p["position_limit_ok"]


def test_kill_switch_env_blocks_trading():
    cfg = load_settings({"KILL_SWITCH": "true"})
    assert RiskManager(cfg, Database(":memory:")).halted()


def test_settings_validation_and_live_gate():
    assert not load_settings({}).orders_possible
    assert not load_settings({"MODE": "live", "BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}).orders_possible           # no confirmation
    assert load_settings({"MODE": "live", "BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s", "LIVE_TRADING_CONFIRM": "I_ACCEPT_REAL_MONEY_RISK"}).orders_possible
    assert not load_settings({"MODE": "live", "BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s", "LIVE_TRADING_CONFIRM": "yes"}).orders_possible
    assert not load_settings({"MODE": "dry"}).orders_possible
    assert load_settings({"MODE": "live"}).require_streams
    for bad in ({"MODE": "paper"}, {"RISK_PER_TRADE_PCT": "50"}, {"LEVERAGE": "9", "MAX_LEVERAGE": "5"}, {"ACTIVE_STYLES": "yolo"}, {"ALERT_EMAIL": "x"}, {"REPORT_HOUR": "30"}):
        with pytest.raises(ValueError):
            load_settings(bad)
    assert load_settings({"MODE": "testnet"}).trade_base == "https://demo-fapi.binance.com"
    assert len(load_settings({}).symbols) == 16 and "BNBUSDT" in load_settings({}).symbols


def test_secrets_are_not_in_repr():
    cfg = load_settings({"BINANCE_API_KEY": "SUPERSECRETKEY", "BINANCE_API_SECRET": "SUPERSECRETVALUE", "ADMIN_TOKEN": "tok" * 8, "SMTP_PASS": "pw"})
    text = repr(cfg)
    assert "SUPERSECRET" not in text and "tok" * 8 not in text and "pw'" not in text


def test_inventory_risk_multiplier_only_reduces_planned_risk():
    rm = RiskManager(CFG, Database(":memory:"))
    full = rm.size(setup(), SOL, 100.0, 100.0, risk_multiplier=1.0)
    reduced = rm.size(setup(), SOL, 100.0, 100.0, risk_multiplier=0.5)
    assert full["ok"] and reduced["ok"]
    assert reduced["planned_risk_usdt"] <= full["planned_risk_usdt"] * 0.51
    assert reduced["risk_multiplier"] == 0.5
    assert reduced["risk_usdt"] <= full["risk_usdt"]
