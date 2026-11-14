import pytest
"""Telegram: notifier robustness, signal publishing (grades, cooldown, levels), events, digest and chat commands."""
from imperion.config import TIMEFRAMES, load_settings
from imperion.fakes import make_engine
from imperion.notify import telegram as tgm
from imperion.runtime.telegram_service import grade_ok

from .helpers import crafted_analyze

TOKEN, CHAT = "123456:SECRET-TOKEN", "4242"
TG = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}


class FakeTelegram:
    """Records sendMessage calls; serves queued updates to getUpdates."""

    def __init__(self):
        self.sent, self.updates, self.fail_with = [], [], None

    def __call__(self, url, payload, timeout):
        assert url.startswith(f"https://api.telegram.org/bot{TOKEN}/")
        if self.fail_with is not None:
            status, body = self.fail_with
            self.fail_with = None
            return status, body
        if url.endswith("/getUpdates"):
            out = [u for u in self.updates if u["update_id"] >= payload["offset"]]
            return 200, {"ok": True, "result": out}
        assert payload["chat_id"] == CHAT and payload["parse_mode"] == "HTML"
        self.sent.append(payload["text"])
        return 200, {"ok": True}

    def text(self):
        return "\n".join(self.sent)


# ---- notifier ------------------------------------------------------------------------------------------
def test_notifier_queue_rate_limit_and_errors():
    cap = FakeTelegram()
    n = tgm.Notifier(TOKEN, CHAT, cap, min_interval=0.0)
    n.send("a"); n.send("b")
    cap.fail_with = (429, {"ok": False, "parameters": {"retry_after": 30}})
    assert n.flush() == 0 and n.pending() == 2, "429 keeps the message queued and pauses sending"
    n._pause_until = 0
    assert n.flush() == 2 and cap.sent == ["a", "b"] and n.pending() == 0
    n.send("bad")
    cap.fail_with = (400, {"ok": False, "description": "can't parse entities"})
    assert n.flush() == 0 and n.pending() == 0 and n.failed == 1, "a rejected message is dropped, not retried forever"

    def boom(url, payload, timeout):
        raise RuntimeError(f"connection failed for {url}")
    n.transport = boom
    n.send("x")
    assert n.flush() == 0 and TOKEN not in n.last_error and TOKEN not in str(n.status())
    big = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
    parts = tgm.split_message(big)
    assert len(parts) > 1 and all(len(p) <= tgm.MAX_LEN for p in parts) and "\n".join(parts) == big
    assert tgm.esc("<b>&") == "&lt;b&gt;&amp;"


def test_commands_only_from_the_configured_chat():
    cap = FakeTelegram()
    n = tgm.Notifier(TOKEN, CHAT, cap, min_interval=0.0)
    cap.updates = [{"update_id": 5, "message": {"chat": {"id": 999}, "text": "/durdur hack"}},
                   {"update_id": 6, "message": {"chat": {"id": int(CHAT)}, "text": "/analiz@imperion_bot sol"}},
                   {"update_id": 7, "message": {"chat": {"id": int(CHAT)}, "text": "merhaba"}}]
    assert n.poll_commands() == [("/analiz", "sol")]
    assert n.poll_commands() == [], "updates are acknowledged through the offset"


def tg_engine(monkeypatch, env=None, side="LONG", playbook="trend_pullback", grade="A", **kw):
    cap = FakeTelegram()
    engine, fake = make_engine(env={**TG, **(env or {})}, telegram_transport=cap, **kw)
    engine.tg.min_interval = 0.0
    monkeypatch.setattr(engine.director, "analyze", crafted_analyze(fake, side, playbook, grade))
    engine.tg.flush()
    cap.sent.clear()                                    # drop the start-up message
    return engine, fake, cap


# ---- signals -------------------------------------------------------------------------------------------
def test_signal_only_mode_publishes_a_complete_signal_without_any_order(monkeypatch):
    engine, fake, cap = tg_engine(monkeypatch, {"LIVE_TRADING_CONFIRM": ""})     # observe-only: no order can be sent
    r = engine.evaluate("SOLUSDT", "day")
    assert r["status"] == "APPROVED_NOT_TRADED"
    engine.tg.flush()
    msg = cap.text()
    for needle in ("SOLUSDT LONG", "DAY TRADE", "Trend geri çekilmesi", "Giriş:", "Stop:", "TP1:", "TP2:", "Ajanlar", "8 zaman dilimi", "<b>A</b>"):
        assert needle in msg, needle
    assert not [c for c in fake.calls if c[0] == "POST"] and TOKEN not in msg
    engine.evaluate("SOLUSDT", "day"); engine.tg.flush()
    assert len(cap.sent) == 1, "one message per (symbol, style, side) per cooldown window"
    with pytest.raises(ValueError, match="DAY_TRADE_ONLY"):
        engine.evaluate("SOLUSDT", "swing")
    engine.tg.flush()
    assert len(cap.sent) == 1, "scalp/swing must not produce signals"
    engine.stop()


def test_short_day_trade_signal_and_traded_tag(monkeypatch):
    engine, fake, cap = tg_engine(monkeypatch, side="SHORT", playbook="sweep_reversal")
    assert engine.evaluate("LINKUSDT", "day")["traded"]
    engine.tg.flush()
    msg = cap.text()
    assert "LINKUSDT SHORT" in msg and "DAY TRADE" in msg and "İŞLEM AÇILDI" in msg and "Likidite süpürmesi" in msg
    engine.stop()


def test_grade_filter_levels_and_off_switch(monkeypatch):
    assert grade_ok("A+", "B") and grade_ok("A", "A") and not grade_ok("B", "A") and not grade_ok("C", "B") and not grade_ok("", "B")
    engine, fake, cap = tg_engine(monkeypatch, {"TELEGRAM_MIN_GRADE": "A+"}, grade="A")
    engine.evaluate("SOLUSDT", "day"); engine.tg.flush()
    assert not cap.sent, "grade A is below TELEGRAM_MIN_GRADE=A+"
    engine.stop()
    engine, fake, cap = tg_engine(monkeypatch, {"TELEGRAM_SIGNALS": "setups", "MAX_CONCURRENT_POSITIONS": "1"})
    assert engine.evaluate("SOLUSDT", "day")["traded"]
    blocked = engine.evaluate("XRPUSDT", "day")
    assert not blocked["traded"] and blocked["analysis_valid"]
    engine.tg.flush()
    assert any("XRPUSDT" in m and "KURULUM" in m and "MAX_POSITIONS" in m for m in cap.sent)
    engine.stop()
    engine, fake, cap = tg_engine(monkeypatch, {"MAX_CONCURRENT_POSITIONS": "1"})
    engine.evaluate("SOLUSDT", "day"); engine.evaluate("XRPUSDT", "day"); engine.tg.flush()
    assert not any("XRPUSDT" in m for m in cap.sent), "default level publishes approved signals only"
    engine.stop()
    engine, fake, cap = tg_engine(monkeypatch, {"TELEGRAM_SIGNALS": "off"})
    engine.evaluate("SOLUSDT", "day"); engine.tg.flush()
    assert not cap.sent
    engine.stop()


def test_signal_formatting_escapes_and_fits():
    res = {"symbol": "BTCUSDT", "style": "swing", "direction": "LONG", "status": "APPROVED_NOT_TRADED", "quality": 86.0, "grade": "A+",
           "net": 0.55, "agree": ["trend", "structure"],
           "setup": {"entry": 65000.5, "stop": 64000.0, "tp1": 66500.0, "tp2": 68000.0, "rr": 3.0, "stop_pct": 1.54, "side": "LONG"},
           "playbook": {"name": "breakout_retest", "side": "LONG", "score": 0.8, "steps": ["<script>alert(1)</script>"]},
           "agents": {"trend": {"d": "BULLISH", "c": 0.8, "ok": True, "summary": "4h yükseliş"}, "momentum": {"d": "BEARISH", "c": 0.4, "ok": True, "summary": "RSI"}},
           "market": {"regime": "TREND_UP", "profile": "TREND", "session": "LONDON", "volatility": "NORMAL"},
           "ai": {"p_win": 0.612, "model_validated": True},
           "mtf": {"tfs": {tf: {"bias": "BULLISH"} for tf in TIMEFRAMES}, "confluence": 0.9, "direction": "BULLISH"}}
    msg = tgm.format_signal(res, "LIVE <test>")
    assert "BTCUSDT LONG</b> · SWING" in msg and "🏆" in msg and "Kırılım + yeniden test" in msg and "61.2%" in msg
    assert "<script>" not in msg and "&lt;script&gt;" in msg and "&lt;test&gt;" in msg
    assert "✅ Trend" in msg and "❌ Momentum" in msg and len(msg) < tgm.MAX_LEN


def test_events_report_digest_and_commands(monkeypatch):
    engine, fake, cap = tg_engine(monkeypatch)
    assert engine.evaluate("SOLUSDT", "day")["traded"]
    engine.halt("test incident")
    engine.telegram.flush_events(); engine.tg.flush()
    msg = cap.text()
    # Tight <=1% fill-risk policy can reject a slipped entry by emergency-close.
    # Either outcome must be delivered as a persisted Telegram trade event.
    assert ("OPENED LONG SOLUSDT" in msg or "SLIPPAGE_ABORT" in msg)
    assert "test incident" in msg and "🚨" in msg
    n = len(cap.sent)
    engine.telegram.flush_events(); engine.tg.flush()
    assert len(cap.sent) == n, "the persisted cursor prevents repeats"
    assert engine.telegram.digest(force=True) and not engine.telegram.digest()
    ok, detail = engine.send_report()
    engine.tg.flush()
    assert ok and "telegram" in detail and any("piyasa analizi" in m for m in cap.sent) and any("günlük rapor" in m for m in cap.sent)
    h = engine.telegram.handle_command
    for cmd, arg, needle in (("/yardim", "", "/analiz"), ("/durum", "", "IMPERION v"), ("/piyasa", "", "piyasa analizi"),
                             ("/sinyaller", "", "SOLUSDT"), ("/analiz", "DOGE2", "Sembol bulunamadı"), ("/analiz", "sol yolo", "Stil:"), ("/x", "", "Bilinmeyen")):
        assert needle in h(cmd, arg), cmd
    engine.stop()


def test_analysis_command_runs_the_real_director(monkeypatch):
    engine, fake, cap = tg_engine(monkeypatch)
    monkeypatch.undo()                                   # use the real Director for this command
    out = engine.telegram.handle_command("/analiz", "sol day")
    assert "SOLUSDT analizi" in out and "Ajanlar" in out and "Karar:" in out and all(tf in out for tf in TIMEFRAMES)
    assert not engine.db.one("SELECT COUNT(*) c FROM shadows")["c"], "an on-demand analysis is read-only"
    engine.stop()


def test_chat_can_halt_but_never_resume(monkeypatch):
    engine, fake, cap = tg_engine(monkeypatch)
    cap.updates = [{"update_id": 1, "message": {"chat": {"id": 1}, "text": "/durdur"}}]
    engine._timers.pop("tg_poll", None); engine.telegram.poll_once()
    assert not engine.risk.halted(), "a foreign chat cannot halt the engine"
    cap.updates = [{"update_id": 2, "message": {"chat": {"id": int(CHAT)}, "text": "/durdur haber"}}]
    engine._timers.pop("tg_poll", None); engine.telegram.poll_once()
    assert "telegram: haber" in engine.risk.halted() and any("durduruldu" in m for m in cap.sent)
    cap.updates = [{"update_id": 3, "message": {"chat": {"id": int(CHAT)}, "text": "/resume"}}]
    engine._timers.pop("tg_poll", None); engine.telegram.poll_once()
    assert engine.risk.halted(), "resume is not a Telegram command"
    st = str(engine.status())
    assert TOKEN not in st and TOKEN not in repr(engine.cfg)
    engine.stop()


def test_settings():
    assert load_settings(TG).telegram_enabled and not load_settings({}).telegram_enabled
    assert load_settings({"TELEGRAM_MIN_GRADE": "a"}).telegram_min_grade == "A"
