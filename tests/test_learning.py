
"""Learning layer: context filter, walk-forward validated win model, agent-weight proposals, migrations."""

import json
import random
import sqlite3

import pytest

from imperion.config import CORE_VERSION, load_settings
from imperion.db import Database
from imperion.learning import model as wm
from imperion.learning.context import ContextStats, quality_bucket
from imperion.learning.learner import ABS_HIGH, ABS_LOW, STEP_CAP, WeightLearner
from imperion.learning.service import LearningService
from imperion.learning.shadow import ShadowTracker
from imperion.learning.walk_forward import make_folds
from imperion.core.policy import AGENT_WEIGHTS
from imperion.util import now_ms


def sample(i, r, style="day", symbol="SOLUSDT", side="LONG", meta=None, evidence=None):
    return {
        "ts": i,
        "r_result": r,
        "style": style,
        "symbol": symbol,
        "side": side,
        "meta": meta or {},
        "evidence": evidence or {},
    }


def labelled(n, seed=3, agent="orderflow"):
    rnd, out = random.Random(seed), []
    for i in range(n):
        agree = rnd.random() < 0.5
        win = rnd.random() < (0.8 if agree else 0.2)
        snap = {
            agent: {
                "d": "BULLISH" if agree else "BEARISH",
                "s": 0.8,
            },
            "context": {
                "d": rnd.choice(["BULLISH", "BEARISH"]),
                "s": 0.5,
            },
        }
        out.append(
            sample(
                i,
                1.5 if win else -1.1,
                evidence=snap,
                meta={"playbook": rnd.choice(wm.PLAYBOOKS)},
            )
        )
    return out


def insert(db, rows, core=CORE_VERSION):
    """Insert labelled samples for the active V17 core by default."""
    for s in rows:
        db.execute(
            "INSERT INTO shadows(ts,symbol,style,side,entry,stop,tp1,tp2,horizon_ts,status,r_result,resolved_ts,evidence_json,meta_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["ts"],
                s["symbol"],
                s["style"],
                s["side"],
                1,
                0.9,
                1.1,
                1.2,
                s["ts"] + 1,
                "TARGET" if s["r_result"] > 0 else "STOP",
                s["r_result"],
                1,
                json.dumps(s["evidence"]),
                json.dumps({"core": core, **s["meta"]}),
            ),
        )


# ---- context filter -------------------------------------------------

def test_context_filter_blocks_only_proven_negative_contexts():
    rnd = random.Random(7)
    bad = [
        sample(
            i,
            rnd.choice([-1.1, -1.1, -1.1, 0.6]),
            meta={
                "regime": "RANGE",
                "session": "ASIA",
                "quality": 76,
                "playbook": "range_fade",
            },
        )
        for i in range(60)
    ]
    good = [
        sample(
            100 + i,
            rnd.choice([1.4, 1.4, -1.1]),
            meta={
                "regime": "TREND_UP",
                "session": "LONDON",
                "quality": 86,
                "playbook": "trend_pullback",
            },
            symbol="LTCUSDT",
        )
        for i in range(60)
    ]
    stats = ContextStats(bad + good)
    v = stats.verdict(
        "day", "RANGE", "SOLUSDT", "LONG",
        "ASIA", 76, 30, "range_fade"
    )
    assert v["blocked"] and any(
        "style_playbook=day|range_fade" in r
        for r in v["reasons"]
    )
    assert not stats.verdict(
        "day", "TREND_UP", "LTCUSDT", "LONG",
        "LONDON", 86, 30, "trend_pullback"
    )["blocked"]
    assert not ContextStats(bad[:12]).verdict(
        "day", "RANGE", "SOLUSDT", "LONG",
        "ASIA", 76, 30
    )["blocked"], "small samples never veto"
    assert stats.worst(30)[0]["expectancy_r"] < 0 < stats.best(30)[0]["expectancy_r"]
    assert quality_bucket(76) == "75-80" and quality_bucket(None) is None


# ---- win model ------------------------------------------------------

def test_features_cover_agents_and_playbooks():
    f = wm.features(
        {"trend": {"d": "BULLISH", "s": 0.8}},
        "SHORT",
        "range_fade",
    )
    assert len(f) == len(wm.FEATURES) == len(AGENT_WEIGHTS) + len(wm.PLAYBOOKS)
    assert f[wm.FEATURES.index("trend")] == pytest.approx(-0.8)
    assert f[wm.FEATURES.index("pb:range_fade")] == 1.0


def test_win_model_walk_forward_validates_real_signal_and_round_trips():
    model, rep = wm.train(labelled(300), 60)
    assert (
        model is not None
        and model.validated
        and rep["status"] == "validated"
        and rep["method"] == "expanding_window_rolling_walk_forward"
    ), rep
    assert (
        rep["metrics"]["auc"] > 0.7
        and rep["metrics"]["brier"] < rep["metrics"]["brier_base"]
        and rep["metrics"]["folds"] >= 2
    )
    hi = model.predict(
        {"orderflow": {"d": "BULLISH", "s": 0.8}}, "LONG"
    )
    lo = model.predict(
        {"orderflow": {"d": "BULLISH", "s": 0.8}}, "SHORT"
    )
    assert hi > 0.6 > 0.4 > lo
    clone = wm.WinModel.from_dict(
        json.loads(json.dumps(model.to_dict()))
    )
    assert clone.validated
    assert clone.predict(
        {"orderflow": {"d": "BULLISH", "s": 0.8}}, "LONG"
    ) == pytest.approx(hi, abs=1e-3)
    assert wm.train(labelled(20), 60)[0] is None
    assert wm.WinModel.from_dict({
        "weights": {"delta": 1.0},
        "bias": 0,
        "validated": True,
    }) is None, "models of an older engine are ignored"


def test_noise_is_never_validated():
    rnd = random.Random(11)
    noise = [
        sample(
            i,
            rnd.choice([1.5, -1.1]),
            evidence={
                "orderflow": {
                    "d": rnd.choice(["BULLISH", "BEARISH"]),
                    "s": 0.7,
                }
            },
        )
        for i in range(300)
    ]
    model, rep = wm.train(noise, 60)
    assert model is not None
    assert not model.validated
    assert rep["status"] == "advisory_only"


def test_walk_forward_folds_are_strictly_chronological():
    folds = make_folds(200, min_train=80, n_splits=4)
    assert len(folds) == 4
    assert all(
        f.train_end == f.test_start
        and f.test_start < f.test_end
        for f in folds
    )
    assert folds[-1].test_end <= 200
    assert make_folds(50, min_train=80) == []


# ---- weight learner -------------------------------------------------

def test_learner_proposals_are_bounded_and_need_manual_approval():
    cfg, db = load_settings({}), Database(":memory:")
    learner = WeightLearner(db, cfg, ShadowTracker(db))
    assert learner.run()["status"] == "insufficient_data"

    rnd = random.Random(5)
    rows = []
    for i in range(160):
        win = rnd.random() < 0.55
        snap = {
            n: {"d": "BULLISH", "s": 0.7}
            for n in (
                "trend", "structure", "liquidity",
                "zones", "trigger"
            )
        }
        snap["orderflow"] = {
            "d": "BULLISH" if win else "BEARISH",
            "s": 0.8,
        }
        rows.append(
            sample(
                now_ms() - (200 - i) * 3_600_000,
                1.5 if win else -1.0,
                evidence=snap,
            )
        )

    insert(db, rows)
    rep = learner.run()

    assert rep["samples"] == 160
    assert rep["status"] in (
        "proposed_manual_review",
        "no_improvement",
        "no_change",
    )
    assert rep["method"] == "expanding_window_rolling_walk_forward"

    folds = rep["metrics"]["folds"]
    assert len(folds) == rep["metrics"]["fold_count"] >= 2

    for a, b in zip(folds, folds[1:]):
        assert a["train_end_ts"] < a["test_start_ts"]
        assert a["train"] < b["train"]

    assert all(
        {"baseline", "proposed", "gain_r", "won"} <= set(f)
        for f in folds
    )

    version, active = learner.active()
    assert version == 0 and active == AGENT_WEIGHTS

    if rep["status"] == "proposed_manual_review":
        v = rep["version"]
        prop = next(
            p for p in learner.proposals()
            if p["version"] == v
        )["weights"]
        for k, w in prop.items():
            assert (
                AGENT_WEIGHTS[k] * ABS_LOW - 1e-9
                <= w
                <= AGENT_WEIGHTS[k] * ABS_HIGH + 1e-9
            )
            assert (
                abs(w - AGENT_WEIGHTS[k])
                <= AGENT_WEIGHTS[k] * STEP_CAP + 1e-9
            )
        assert learner.approve(v)
        assert learner.active()[0] == v
        assert learner.rollback() == 0
        assert learner.active()[0] == 0


def test_weight_rows_of_older_engines_are_ignored():
    cfg, db = load_settings({}), Database(":memory:")
    v = db.add_weights(
        {"delta": 2.0, "cvd": 2.0},
        "active",
        None,
        "v12 engine weights",
    )
    assert v
    assert WeightLearner(
        db, cfg, ShadowTracker(db)
    ).active() == (0, AGENT_WEIGHTS)


# ---- service --------------------------------------------------------

def test_service_cycle_trains_and_persists():
    cfg, db = load_settings({}), Database(":memory:")
    svc = LearningService(cfg, db, ShadowTracker(db))
    insert(db, labelled(160))

    rep = svc.cycle()

    assert rep["win_model"]["status"] in (
        "validated", "advisory_only"
    )
    assert svc.model is not None
    assert rep["promotion_policy"] == "MANUAL_APPROVAL_ONLY"
    assert "worst" in rep["contexts"]
    assert db.kv_get("ai.model")["feature_set"] == wm.FEATURE_SET
    assert LearningService(
        cfg, db, ShadowTracker(db)
    ).model is not None
    assert svc.summary()["win_model"]
    assert set(svc.summary()["performance"]) >= {
        "shadow", "dry", "testnet", "live"
    }


def test_old_database_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE shadows ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts INTEGER, symbol TEXT, style TEXT, side TEXT, "
        "entry REAL, stop REAL, tp1 REAL, tp2 REAL, "
        "horizon_ts INTEGER, status TEXT, r_result REAL, "
        "resolved_ts INTEGER, evidence_json TEXT, "
        "decision_id INTEGER, source TEXT)"
    )
    con.execute(
        "INSERT INTO shadows("
        "ts,symbol,style,side,status,r_result,evidence_json"
        ") VALUES("
        "1,'SOLUSDT','day','LONG','STOP',-1.0,'{}'"
        ")"
    )
    con.commit()
    con.close()

    for _ in range(2):
        db = Database(path)
        assert "meta_json" in {
            r["name"]
            for r in db.query("PRAGMA table_info(shadows)")
        }
        db.close()

    tracker = ShadowTracker(Database(path))
    assert tracker.resolved() == []
    assert tracker.resolved(core=None)[0]["meta"] == {}


def test_context_filter_only_judges_approved_setups():
    cfg, db = load_settings({}), Database(":memory:")
    svc = LearningService(cfg, db, ShadowTracker(db))

    rows = [
        sample(
            i,
            -1.1,
            meta={
                "approved": False,
                "playbook": "range_fade",
            },
        )
        for i in range(80)
    ]

    insert(db, rows)

    assert svc.context_stats().n_samples == 0
    assert len(svc.shadows.resolved()) == 80


def test_weight_proposal_requires_out_of_sample_wins_in_most_folds():
    """An early-only edge must not produce an approved-looking proposal."""
    cfg, db = load_settings({}), Database(":memory:")
    learner = WeightLearner(db, cfg, ShadowTracker(db))

    rnd = random.Random(9)
    rows = []

    for i in range(200):
        early = i < 80
        agree = rnd.random() < 0.5
        win = rnd.random() < (
            (0.85 if agree else 0.15) if early else 0.5
        )
        snap = {
            n: {"d": "BULLISH", "s": 0.7}
            for n in (
                "trend", "structure", "liquidity",
                "zones", "trigger"
            )
        }
        snap["orderflow"] = {
            "d": "BULLISH" if agree else "BEARISH",
            "s": 0.8,
        }
        rows.append(
            sample(
                i * 1000,
                1.5 if win else -1.0,
                evidence=snap,
            )
        )

    insert(db, rows)
    rep = learner.run()

    assert (
        rep["status"] != "proposed_manual_review"
        or rep["metrics"]["folds_won"]
        > rep["metrics"]["fold_count"] / 2
    )
    assert learner.active()[0] == 0


def test_performance_classes_are_never_pooled():
    from imperion.learning.performance import performance_report

    shadows = [
        {"r_result": 1.0},
        {"r_result": -1.0},
        {"r_result": 2.0},
    ]
    trades = [
        {
            "mode": "live",
            "r_multiple": 1.5,
            "pnl_usdt": 1.4,
        },
        {
            "mode": "live",
            "r_multiple": None,
            "pnl_usdt": None,
        },
        {
            "mode": "testnet",
            "r_multiple": -1.0,
            "pnl_usdt": -1.0,
        },
        {
            "mode": "dry",
            "r_multiple": 0.5,
            "pnl_usdt": 0.4,
        },
    ]

    rep = performance_report(shadows, trades)

    assert rep["shadow"]["samples"] == 3
    assert rep["shadow"]["expectancy_r"] == pytest.approx(
        2 / 3, abs=1e-4
    )
    assert rep["live"]["samples"] == 1
    assert rep["live"]["unverified"] == 1
    assert rep["live"]["pnl_usdt"] == 1.4
    assert rep["testnet"]["samples"] == 1
    assert rep["testnet"]["expectancy_r"] == -1.0
    assert rep["dry"]["samples"] == 1
    assert "pnl_usdt" not in rep["shadow"]
