
"""Daily reports, mail configuration, and conservative shadow outcomes."""

import json
from datetime import datetime, timezone

import pytest

from imperion.config import CORE_VERSION, load_settings
from imperion.db import Database
from imperion.learning.shadow import ShadowTracker, simulate_outcome
from imperion.models import Candle
from imperion.reports import daily
from imperion.util import now_ms


def ts(y, m, d, h, mi=0):
    return int(
        datetime(
            y, m, d, h, mi, tzinfo=timezone.utc
        ).timestamp() * 1000
    )


# Daily report schedule: 08:00 Europe/Istanbul = 05:00 UTC.

def test_report_is_due_after_0800_istanbul_once_per_day():
    cfg, db = load_settings({}), Database(":memory:")

    assert daily.report_due(
        cfg, db, ts(2026, 11, 10, 4, 59)
    ) is None

    assert daily.report_due(
        cfg, db, ts(2026, 11, 10, 5, 0)
    ) == "2026-11-10"

    db.mark_report("2026-11-10", False, "smtp down")

    assert daily.report_due(
        cfg, db, ts(2026, 11, 10, 6, 0)
    ) == "2026-11-10"

    db.mark_report("2026-11-10", True, "sent")

    assert daily.report_due(
        cfg, db, ts(2026, 11, 10, 6, 0)
    ) is None

    assert daily.report_due(
        cfg, db, ts(2026, 11, 10, 20, 0)
    ) is None

    assert daily.report_due(
        cfg, db, ts(2026, 11, 11, 5, 1)
    ) == "2026-11-11"


def test_next_report_time():
    cfg = load_settings({})

    n = daily.next_report_time(
        cfg, ts(2026, 11, 10, 6, 0)
    )
    assert (n.hour, n.day) == (8, 11)

    n = daily.next_report_time(
        cfg, ts(2026, 11, 10, 3, 0)
    )
    assert (n.hour, n.day) == (8, 10)

    assert load_settings({
        "REPORT_HOUR": "9"
    }).report_hour == 9


def test_render_contains_all_sections_and_escapes_html():
    cfg, db = load_settings({}), Database(":memory:")

    db.add_equity(100.0, 90.0, 0.0, "t")
    db.add_equity(101.5, 90.0, 0.0, "t")

    db.upsert_trade({
        "id": "t1",
        "ts_open": now_ms() - 3600_000,
        "ts_close": now_ms() - 600_000,
        "symbol": "SOLUSDT",
        "style": "day",
        "side": "LONG",
        "entry": 140.0,
        "exit_price": 143.0,
        "qty": 1,
        "stop": 138,
        "tp1": 142,
        "tp2": 145,
        "risk_usdt": 1.0,
        "status": "CLOSED",
        "pnl_usdt": 1.4,
        "fees_usdt": 0.1,
        "r_multiple": 1.4,
        "exit_reason": "<script>x</script>",
    })

    db.add_decision({
        "symbol": "SOLUSDT",
        "style": "day",
        "direction": "LONG",
        "approved": False,
        "status": "BLOCKED",
        "reasons": [
            "NO_ROOM (x)",
            "COST_EXCEEDS_EDGE",
        ],
        "data_ok": True,
    })

    data = daily.gather(
        cfg,
        db,
        {
            "symbols": {
                "SOLUSDT": {
                    "price": 140.1,
                    "mtf": "BULLISH",
                    "regime": "TREND_UP",
                    "direction": "LONG",
                    "grade": "A",
                    "playbook": "trend_pullback",
                    "whale_net_15m": 250000.0,
                    "book_imbalance": 0.2,
                    "funding_pct": 0.01,
                }
            },
            "radar": [{
                "symbol": "HYPEUSDT",
                "quote_volume": 9e8,
                "bias_1h": "BULLISH",
                "whale_net_15m": -1e5,
            }],
            "ai": {
                "active_version": 0,
                "win_model": {
                    "validated": True,
                    "metrics": {
                        "auc": 0.61,
                        "brier": 0.23,
                    },
                },
            },
            "risk": {},
            "health": {},
            "market": {
                "session": "LONDON",
                "breadth": 0.7,
                "breadth_direction": "BULLISH",
                "leader_bias": "BULLISH",
            },
        },
    )

    subject, text, html = daily.render(data)

    assert "IMPERION" in subject
    assert "+1.40 USDT" in subject

    for section in (
        "ÖZET",
        "KAPANAN İŞLEMLER",
        "SEMBOL DURUMU",
        "TARAMA LİSTESİ",
        "PİYASA GENEL GÖRÜNÜMÜ",
        "ENGELLENEN SİNYAL",
        "YAPAY ZEKA",
        "RİSK DURUMU",
        "SİSTEM SAĞLIĞI",
    ):
        assert section in text, section

    assert "<script>" not in html
    assert "&lt;script&gt;" in html

    assert "NO_ROOM" in text
    assert "kâr garantisi" in text
    assert "trend_pullback" in text
    assert "AUC 0.61" in text


def test_send_mail_requires_configuration():
    ok, detail = daily.send_mail(
        load_settings({}), "s", "t"
    )

    assert not ok
    assert "not configured" in detail


# Conservative shadow-trading outcomes.

def C(t, o, h, l, c):
    return Candle(
        t, o, h, l, c, 1.0, t + 59_999
    )


def test_shadow_outcomes_are_conservative():
    long_ = {
        "side": "LONG",
        "entry": 100.0,
        "stop": 98.0,
        "tp1": 102.4,
        "tp2": 105.0,
    }

    assert simulate_outcome(
        **long_,
        candles=[
            C(0, 100, 101, 99, 100.5),
            C(1, 100.5, 101.5, 100, 101),
        ],
    ) is None

    r = simulate_outcome(
        **long_,
        candles=[
            C(0, 100, 101, 97.9, 99)
        ],
    )
    assert r["status"] == "STOP"
    assert r["r"] == -1.0

    # Stop and target in the same candle: stop first.
    r = simulate_outcome(
        **long_,
        candles=[
            C(0, 100, 106, 97, 105)
        ],
    )
    assert r["status"] == "STOP"

    r = simulate_outcome(
        **long_,
        candles=[
            C(0, 100, 102.5, 99.5, 102),
            C(1, 102, 105.5, 101, 105),
        ],
    )
    assert r["status"] == "TARGET"
    assert r["r"] == pytest.approx(
        0.5 * 1.2 + 0.5 * 2.5
    )

    r = simulate_outcome(
        **long_,
        candles=[
            C(0, 100, 102.5, 99.5, 102),
            C(1, 102, 103, 99.9, 100),
        ],
    )
    assert r["status"] == "BE_AFTER_TP1"
    assert r["r"] == pytest.approx(0.6)

    short = {
        "side": "SHORT",
        "entry": 100.0,
        "stop": 102.0,
        "tp1": 97.6,
        "tp2": 95.0,
    }

    assert simulate_outcome(
        **short,
        candles=[
            C(0, 100, 102.1, 99, 101)
        ],
    )["status"] == "STOP"

    assert simulate_outcome(
        **short,
        candles=[
            C(0, 100, 101, 97.5, 98),
            C(1, 98, 98.5, 94.9, 95),
        ],
    )["status"] == "TARGET"

    cost = simulate_outcome(
        **long_,
        candles=[
            C(0, 100, 101, 97.9, 99)
        ],
        cost_r=0.08,
    )
    assert cost["r"] == pytest.approx(-1.08)


def test_shadow_tracker_resolves_from_klines():
    db = Database(":memory:")
    tracker = ShadowTracker(db, fetch=None)

    snap = {
        "orderflow": {
            "d": "BULLISH",
            "s": 0.7,
        },
        "momentum": {
            "d": "BEARISH",
            "s": 0.5,
        },
    }

    sid = tracker.record(
        None,
        "SOLUSDT",
        "day",
        "LONG",
        100.0,
        98.0,
        102.4,
        105.0,
        snap,
        "test",
        now=now_ms() - 3_600_000,
        meta={
            "playbook": "sweep_reversal"
        },
    )

    row = db.one(
        "SELECT * FROM shadows WHERE id=?",
        (sid,),
    )

    assert json.loads(
        row["evidence_json"]
    ) == snap

    # Match the active V17 core, not a hardcoded V16 identity.
    assert json.loads(row["meta_json"]) == {
        "core": CORE_VERSION,
        "playbook": "sweep_reversal",
    }

    t0 = (
        row["ts"] // 300_000 + 1
    ) * 300_000

    rows = [
        [
            t0 + i * 300_000,
            "100",
            "103" if i == 0 else "106",
            "99.5" if i == 0 else "100.5",
            "102",
            "1",
            t0 + i * 300_000 + 299_999,
            "1",
            1,
            "0.5",
            "0",
            "0",
        ]
        for i in range(2)
    ]

    tracker.fetch = (
        lambda sym, tf, start, limit: rows
    )

    assert tracker.resolve_open(0.16) == 1

    done = db.one(
        "SELECT * FROM shadows WHERE id=?",
        (sid,),
    )

    assert done["status"] == "TARGET"
    assert done["r_result"] > 1.5

    assert tracker.resolved()[0][
        "meta"
    ]["playbook"] == "sweep_reversal"

    # Legacy rows must remain excluded from current-core learning.
    db.execute(
        "INSERT INTO shadows("
        "ts,symbol,style,side,status,"
        "r_result,evidence_json,meta_json"
        ") VALUES("
        "1,'SOLUSDT','day','LONG',"
        "'STOP',-1,'{}','{}'"
        ")"
    )

    assert len(tracker.resolved()) == 1
    assert len(
        tracker.resolved(core=None)
    ) == 2, "rows of older engines never train this core"
