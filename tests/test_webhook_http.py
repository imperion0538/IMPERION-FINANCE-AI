import json
import threading
import urllib.error
import urllib.request

import pytest

from imperion.fakes import make_engine
from imperion.server.http import make_server
from imperion.server.webhook import (WebhookWorker, idempotency_key, normalize_alert, normalize_symbol, normalize_timeframe, parse_body,
                                     styles_for)
from imperion.util import now_ms

SECRET, ADMIN = "tv-secret-0123456789", "admin-token-0123456789"


# ------------------------------------------------------------------ pure helpers
def test_symbol_and_timeframe_normalisation():
    assert normalize_symbol("BINANCE:BTCUSDT.P") == "BTCUSDT" and normalize_symbol("ethusdtperp") == "ETHUSDT"
    assert normalize_timeframe("60") == "1h" and normalize_timeframe("240") == "4h" and normalize_timeframe("D") == "1d" and normalize_timeframe("15") == "15m"
    assert normalize_timeframe("7") is None and normalize_timeframe(None) is None


def test_body_parsing_json_and_plain_text():
    assert parse_body(b'{"symbol":"BTCUSDT","action":"buy"}')["symbol"] == "BTCUSDT"
    assert parse_body(b"SOLUSDT long 15") == {"symbol": "SOLUSDT", "action": "long", "timeframe": "15"}
    with pytest.raises(ValueError):
        parse_body(b"???")


def test_alert_validation():
    syms = ("BTCUSDT", "SOLUSDT")
    a = normalize_alert({"symbol": "BINANCE:SOLUSDT.P", "action": "SELL", "interval": "15", "price": "140.2"}, syms)
    assert a["symbol"] == "SOLUSDT" and a["side"] == "SHORT" and a["timeframe"] == "15m" and a["price"] == 140.2
    assert normalize_alert({"symbol": "BTCUSDT", "action": "close"}, syms)["side"] == "CLOSE"
    for bad in ({"symbol": "BNBUSDT", "action": "buy"}, {"symbol": "BTCUSDT", "action": "moon"}, {"action": "buy"}):
        with pytest.raises(ValueError):
            normalize_alert(bad, syms)


def test_idempotency_key_stable_within_minute_or_by_id():
    a = {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "15m", "received_ms": 1_700_000_000_000, "alert_id": None}
    b = {**a, "received_ms": a["received_ms"] + 20_000}
    c = {**a, "received_ms": a["received_ms"] + 90_000}
    assert idempotency_key(a) == idempotency_key(b) != idempotency_key(c)
    assert idempotency_key({**a, "alert_id": "abc"}) == "tv:abc"


def test_style_routing_by_timeframe():
    active = ["scalp", "day", "swing"]
    assert styles_for({"timeframe": "1m"}, active) == ["scalp"]                      # scalp trigger
    assert styles_for({"timeframe": "5m"}, active) == ["scalp", "day"]               # scalp setup + day trigger
    assert styles_for({"timeframe": "1h"}, active) == ["swing"]                      # swing trigger
    assert styles_for({"timeframe": "4h"}, active) == ["swing"]
    assert styles_for({"timeframe": "1w"}, active) == active                       # no style uses it as setup/trigger -> evaluate all
    assert styles_for({"timeframe": None}, active) == active


# ------------------------------------------------------------------ live HTTP server
@pytest.fixture()
def server():
    bot, fake = make_engine(start=False)
    srv = make_server(bot.cfg, bot.db, bot, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def call(method, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode()),
                                     method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    yield bot, call
    srv.shutdown()
    srv.server_close()
    bot.stop()


def test_health_is_public_reports_startup_readiness_and_leaks_nothing_sensitive(server):
    bot, call = server
    code, body = call("GET", "/health")
    assert code == 503 and not body["ok"] and not body["ready"]
    assert "api" not in json.dumps(body).lower()
    assert call("GET", "/status")[0] == 401


def test_webhook_authentication(server):
    bot, call = server
    alert = {"symbol": "SOLUSDT", "action": "buy", "timeframe": "15"}
    assert call("POST", "/webhook/tradingview", alert)[0] == 401
    assert call("POST", "/webhook/tradingview", alert, {"Authorization": "Bearer wrong-secret-value-123"})[0] == 401
    assert call("POST", "/webhook/tradingview", alert, {"X-Webhook-Secret": SECRET})[0] == 200
    code, body = call("POST", "/webhook/tradingview", {**alert, "secret": SECRET, "alert_id": "a-1"})      # secret inside the JSON body
    assert code == 200 and body["accepted"]
    assert call("POST", "/webhook/tradingview", {**alert, "secret": SECRET, "alert_id": "a-1"})[1]["duplicate"] is True
    assert call("POST", "/webhook/tradingview", {"symbol": "PEPEUSDT", "action": "buy", "secret": SECRET})[0] == 400
    assert call("POST", "/webhook/tradingview", b"x" * 100_000, {"X-Webhook-Secret": SECRET})[0] == 413


def test_webhook_disabled_without_strong_secret():
    bot, fake = make_engine(env={"TRADINGVIEW_WEBHOOK_SECRET": "short"}, start=False)
    srv = make_server(bot.cfg, bot.db, bot, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/webhook/tradingview", data=b'{"symbol":"SOLUSDT","action":"buy"}', method="POST",
                                 headers={"X-Webhook-Secret": "short"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 503
    srv.shutdown(); srv.server_close(); bot.stop()


def test_brute_force_lockout(server):
    bot, call = server
    codes = [call("POST", "/webhook/tradingview", {"symbol": "SOLUSDT", "action": "buy"}, {"X-Webhook-Secret": f"guess-{i:016d}"})[0] for i in range(25)]
    assert codes[0] == 401 and codes[-1] == 429
    assert call("POST", "/webhook/tradingview", {"symbol": "SOLUSDT", "action": "buy"}, {"X-Webhook-Secret": SECRET})[0] == 429


def test_admin_endpoints(server):
    bot, call = server
    auth = {"Authorization": f"Bearer {ADMIN}"}
    assert call("POST", "/admin/halt?reason=maintenance", b"", {})[0] == 401
    code, body = call("POST", "/admin/halt?reason=maintenance", b"", auth)
    assert code == 200 and bot.risk.halted() == "maintenance"
    assert call("GET", "/status", None, auth)[1]["trading_enabled"] is False
    assert call("POST", "/admin/resume", b"", auth)[0] == 200 and not bot.risk.halted()
    assert call("POST", "/admin/ai/run", b"", auth)[1]["status"] == "insufficient_data"
    assert call("POST", "/admin/ai/approve?version=99", b"", auth)[1]["approved"] is False
    assert call("POST", "/admin/ai/rollback", b"", auth)[0] == 200
    assert call("POST", "/admin/report", b"", auth)[0] in (200, 502)
    assert call("POST", "/admin/nope", b"", auth)[0] == 404


def test_webhook_worker_evaluates_queued_alerts_and_ignores_stale_or_close():
    bot, fake = make_engine()
    seen = []
    worker = WebhookWorker(bot.cfg, bot.db, lambda sym, style, hint=None, source=None: seen.append((sym, style, hint, source)) or {"direction": "NO TRADE", "reasons": []})
    for i, (side, age) in enumerate((("LONG", 0), ("CLOSE", 0), ("SHORT", 3_600_000))):
        a = {"symbol": "SOLUSDT", "side": side, "timeframe": "15m", "alert_id": f"id{i}", "strategy": "", "price": None, "received_ms": now_ms() - age}
        bot.db.enqueue_webhook(idempotency_key(a), a)
    assert worker.process_pending() == 3
    assert seen and all(s[0] == "SOLUSDT" and s[2] == "LONG" and s[3] == "tradingview" for s in seen)
    statuses = {r["status"] for r in bot.db.query("SELECT status FROM webhook_queue")}
    assert statuses == {"PROCESSED", "IGNORED", "STALE"}
    bot.stop()
