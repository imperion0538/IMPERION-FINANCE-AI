"""Testnet -> Live gate (HMAC-signed evidence + hash-bound approval) and the Testnet validator (strict fake exchange)."""
import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from imperion import cli, gate
from imperion.config import load_settings
from imperion.db import Database
from imperion.exchange.rest import RestClient
from imperion.fakes import TEST_EVIDENCE_SECRET as SECRET, FakeBinance, make_engine, passed_testnet_report as passed
from imperion.risk.liquidation import isolated_liquidation_price
from imperion.testnet import TestnetValidator
from imperion.util import now_ms

from .helpers import crafted_analyze

ROOT = Path(__file__).resolve().parents[1]
LIVE = load_settings({"MODE": "live", "TESTNET_EVIDENCE_SECRET": SECRET})
OTHER_SECRET = "another-evidence-secret-0123456789abcdefgh"


def approve(db, secret=SECRET):
    return gate.approve_live(db, "ops", gate.APPROVAL_PHRASE, 30, secret)


def test_gate_requires_signed_complete_evidence_and_bound_manual_approval():
    db = Database(":memory:")
    st = gate.live_gate_status(db, LIVE)
    assert st["applies"] and not st["ok"] and any("NO_TESTNET_EVIDENCE" in r for r in st["reasons"])
    assert gate.record_testnet_evidence(db, {"passed": False, "checks": [{"name": "clock", "ok": False}]}, SECRET) is None
    gate.record_testnet_evidence(db, passed(order_lifecycle=False), SECRET)
    with pytest.raises(ValueError, match="ORDER_LIFECYCLE"):
        approve(db)
    ev = gate.record_testnet_evidence(db, passed(), SECRET)
    assert ev["signature"] and ev["sig_alg"] == "HMAC-SHA256" and ev["key_id"] == gate.key_id(SECRET) and SECRET not in json.dumps(ev)
    with pytest.raises(ValueError, match="confirm"):
        gate.approve_live(db, "ops", "yes", 30, SECRET)
    assert any("NO_MANUAL_APPROVAL" in r for r in gate.live_gate_status(db, LIVE)["reasons"])
    ap = approve(db)
    assert ap["evidence_hash"] == gate.evidence_hash(ev)
    st = gate.live_gate_status(db, LIVE)
    assert st["ok"] and st["evidence"]["signed"] and st["evidence"]["position_mode"] == "ONE_WAY"
    gate.record_testnet_evidence(db, passed(), SECRET)                # new evidence -> the old approval no longer matches
    assert any("APPROVAL_FOR_OTHER_EVIDENCE" in r for r in gate.live_gate_status(db, LIVE)["reasons"])
    approve(db)
    gate.revoke_live(db, "ops")
    assert not gate.live_gate_status(db, LIVE)["ok"]
    assert not gate.live_gate_status(db, load_settings({}))["applies"]


def test_gate_evidence_expires_and_rejects_future_dates():
    db = Database(":memory:")
    gate.record_testnet_evidence(db, passed(ts=now_ms() - 40 * gate.DAY_MS), SECRET)
    with pytest.raises(ValueError, match="EXPIRED"):
        approve(db)
    gate.record_testnet_evidence(db, passed(ts=now_ms() + 3_600_000), SECRET)
    with pytest.raises(ValueError, match="FUTURE_DATED"):
        approve(db)


@pytest.mark.parametrize("field,value", [("symbol", "ETHUSDT"), ("ts", None), ("host", "fapi.binance.com"), ("checks", "add_one"),
                                         ("liquidation", "edit"), ("version", "14.9.9")])
def test_any_edit_of_signed_evidence_closes_the_gate(field, value):
    """Each edit alone (everything else still valid) must be caught by the signature, not by another rule."""
    db = Database(":memory:")
    gate.record_testnet_evidence(db, passed(), SECRET)
    approve(db)
    assert gate.live_gate_status(db, LIVE)["ok"]
    ev = db.kv_get(gate.EVIDENCE_KEY)
    if field == "ts":
        ev["ts"] -= 1
    elif field == "checks":
        ev["checks"] = ev["checks"] + ["extra_check"]
    elif field == "liquidation":
        ev["liquidation"] = {**ev["liquidation"], "exchange_liq_price": ev["liquidation"]["exchange_liq_price"] * 0.9}
    else:
        ev[field] = value
    db.kv_set(gate.EVIDENCE_KEY, ev)
    reasons = gate.live_gate_status(db, LIVE)["reasons"]
    assert any("SIGNATURE_INVALID" in r for r in reasons), reasons


def test_signature_key_secret_and_format_rules():
    db = Database(":memory:")
    assert gate.record_testnet_evidence(db, passed(), "") is None, "no secret -> no evidence"
    assert gate.record_testnet_evidence(db, passed(), "short") is None
    gate.record_testnet_evidence(db, passed(), OTHER_SECRET)
    assert any("SIGNED_WITH_OTHER_KEY" in r for r in gate.live_gate_status(db, LIVE)["reasons"])
    assert any("SECRET_MISSING" in r for r in gate.live_gate_status(db, load_settings({"MODE": "live"}))["reasons"])
    ev = db.kv_get(gate.EVIDENCE_KEY)
    db.kv_set(gate.EVIDENCE_KEY, {k: v for k, v in ev.items() if k != "signature"})
    assert any("UNSIGNED" in r for r in gate.verify_evidence(db.kv_get(gate.EVIDENCE_KEY), OTHER_SECRET, 30))
    old = {"run_id": "x", "ts": now_ms(), "version": "14.0.0", "order_lifecycle": True, "checks": ["order_lifecycle"]}
    assert any("OLD_FORMAT" in r for r in gate.verify_evidence(old, SECRET, 30)), "v14.0.0 unsigned evidence no longer opens the gate"
    with pytest.raises(ValueError, match="32"):
        load_settings({"TESTNET_EVIDENCE_SECRET": "too-short"})


@pytest.mark.parametrize("over,reason", [
    ({"checks": [{"name": n, "ok": True} for n in gate.REQUIRED_CHECKS if n != "exchange_liquidation"]}, "INCOMPLETE"),
    ({"checks": [{"name": n, "ok": True} for n in gate.REQUIRED_CHECKS if n != "position_mode"]}, "INCOMPLETE"),
    ({"position_mode": "HEDGE"}, "POSITION_MODE"),
    ({"liquidation": {"ok": False, "exchange_liq_price": 0.0}}, "NO_EXCHANGE_LIQUIDATION"),
    ({"liquidation": None}, "NO_EXCHANGE_LIQUIDATION"),
])
def test_signed_but_incomplete_evidence_cannot_be_approved(over, reason):
    db = Database(":memory:")
    gate.record_testnet_evidence(db, passed(**over), SECRET)
    with pytest.raises(ValueError, match=reason):
        approve(db)


def test_live_engine_sends_no_order_while_the_gate_is_closed(monkeypatch):
    engine, fake = make_engine(live_gate=False)
    monkeypatch.setattr(engine.director, "analyze", crafted_analyze(fake))
    ok, why = engine.can_trade()
    assert not ok and why.startswith("LIVE_GATE_CLOSED")
    r = engine.evaluate("SOLUSDT", "day")
    assert r["status"] == "APPROVED_NOT_TRADED" and not fake.pos and not [c for c in fake.calls if c[0] == "POST" and "order" in c[1].lower()]
    assert any(e["kind"] == "live_gate" and e["level"] == "CRITICAL" for e in engine.db.events_since(0))
    gate.record_testnet_evidence(engine.db, passed(), SECRET)
    approve(engine.db)
    assert engine.can_trade()[0] and engine.evaluate("SOLUSDT", "day")["traded"]
    assert engine.status()["live_gate"]["ok"]
    ev = engine.db.kv_get(gate.EVIDENCE_KEY)
    engine.db.kv_set(gate.EVIDENCE_KEY, {**ev, "symbol": "ETHUSDT"})          # tampered while running
    ok, why = engine.can_trade()
    assert not ok and "SIGNATURE_INVALID" in why
    engine.stop()
    testnet, _ = make_engine(env={"MODE": "testnet"}, live_gate=False)
    assert testnet.can_trade()[0], "the gate only applies to real money"
    testnet.stop()


def _validator(fake, out, **env):
    cfg = load_settings({"MODE": "testnet", "BINANCE_API_KEY": fake.api_key, "BINANCE_API_SECRET": fake.api_secret, **env})
    rest = RestClient(cfg.trade_base, cfg.api_key, cfg.api_secret, transport=fake.transport)
    ws = lambda url, receive_message: {"e": "aggTrade"} if receive_message else None     # noqa: E731
    return TestnetValidator(cfg, rest, ws, out.append)


def _orders(fake):
    return [c for c in fake.calls if c[0] == "POST" and c[1] in ("/fapi/v1/order", "/fapi/v1/algoOrder")]


def test_testnet_validator_read_only_and_order_lifecycle():
    fake, out = FakeBinance(), []
    rep = _validator(fake, out).run("SOLUSDT")
    assert rep["passed"] and not rep["order_lifecycle"] and not _orders(fake)
    assert {c["name"] for c in rep["checks"]} == {"clock", "market_data", "signed_rest", "position_mode", "market_websocket", "user_stream_handshake"}
    assert rep["position_mode"] == "ONE_WAY"
    db = Database(":memory:")
    gate.record_testnet_evidence(db, rep, SECRET)
    with pytest.raises(ValueError, match="ORDER_LIFECYCLE"):
        approve(db)
    fake, out = FakeBinance(), []
    rep = _validator(fake, out).run("SOLUSDT", order_smoke=True, qty=Decimal("0.5"), max_notional=100)
    assert rep["passed"] and rep["order_lifecycle"], out
    assert [c["name"] for c in rep["checks"]][-2:] == ["order_lifecycle", "exchange_liquidation"]
    liq = rep["liquidation"]
    assert liq["ok"] and liq["exchange_liq_price"] > 0 and liq["stop_to_liq"] < 0.70 and abs(liq["model_optimism_pct"]) < 0.01, liq
    assert not fake.pos and not fake.algos, "the smoke test cleans up after itself"
    ev = gate.record_testnet_evidence(db, rep, SECRET)
    assert set(gate.REQUIRED_CHECKS) <= set(ev["checks"]) and approve(db) and gate.live_gate_status(db, LIVE)["ok"]


@pytest.mark.parametrize("attr,detail", [("dual", "Hedge"), ("multi_assets", "Multi-Assets")])
def test_testnet_validator_requires_one_way_single_asset_mode(attr, detail):
    fake, out = FakeBinance(), []
    setattr(fake, attr, True)
    rep = _validator(fake, out).run("SOLUSDT", order_smoke=True, qty=Decimal("0.5"), max_notional=100)
    checks = {c["name"]: c for c in rep["checks"]}
    assert not rep["passed"] and not checks["position_mode"]["ok"] and detail in checks["position_mode"]["detail"]
    assert not checks["order_lifecycle"]["ok"] and not checks["exchange_liquidation"]["ok"]
    assert not _orders(fake), "no order is sent to an account in the wrong position / margin mode"
    db = Database(":memory:")
    assert gate.record_testnet_evidence(db, rep, SECRET) is None


def _liq_case(fake, kind):
    px = fake.price("SOLUSDT")
    model = isolated_liquidation_price("LONG", px, 0.5, 3, 0.01)
    return {"missing": 0.0, "too_close": px * 0.975, "model_optimistic": model + 0.012 * px}[kind]


@pytest.mark.parametrize("kind,detail", [("missing", "liquidationPrice missing"), ("too_close", "of the distance"),
                                         ("model_optimistic", "model is optimistic")])
def test_testnet_validator_checks_the_real_liquidation_price(kind, detail):
    fake, out = FakeBinance(), []
    fake.liq_override["SOLUSDT"] = _liq_case(fake, kind)
    rep = _validator(fake, out).run("SOLUSDT", order_smoke=True, qty=Decimal("0.5"), max_notional=100)
    checks = {c["name"]: c for c in rep["checks"]}
    assert checks["order_lifecycle"]["ok"], "the position is still closed and cleaned up"
    assert not checks["exchange_liquidation"]["ok"] and detail in checks["exchange_liquidation"]["detail"], checks["exchange_liquidation"]
    assert not rep["passed"] and not fake.pos and not fake.algos
    assert gate.record_testnet_evidence(Database(":memory:"), rep, SECRET) is None


def test_testnet_validator_refuses_unsafe_smoke_tests():
    fake, out = FakeBinance(), []
    fake.pos["SOLUSDT"] = {"qty": 1.0, "entry": fake.price("SOLUSDT")}
    rep = _validator(fake, out).run("SOLUSDT", order_smoke=True, qty=Decimal("0.5"))
    checks = {c["name"]: c for c in rep["checks"]}
    assert not rep["passed"] and "already has a position" in checks["order_lifecycle"]["detail"]
    assert "not reached" in checks["exchange_liquidation"]["detail"]
    assert fake.pos["SOLUSDT"]["qty"] == 1.0, "an existing position is never touched"
    rep = _validator(FakeBinance(), []).run("SOLUSDT", order_smoke=True, qty=Decimal("50"), max_notional=100)
    assert not rep["passed"] and "notional" in {c["name"]: c for c in rep["checks"]}["order_lifecycle"]["detail"]


def test_cli_gate_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IMPERION_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("TESTNET_EVIDENCE_SECRET", SECRET)
    assert cli.main(["gate-status"]) == 1
    assert cli.main(["approve-live", "--operator", "ops", "--confirm", gate.APPROVAL_PHRASE]) == 1
    gate.record_testnet_evidence(Database(str(tmp_path / "imperion.db")), passed(), SECRET)
    assert cli.main(["approve-live", "--operator", "ops"]) == 1
    assert cli.main(["approve-live", "--operator", "ops", "--confirm", gate.APPROVAL_PHRASE]) == 0
    assert cli.main(["gate-status"]) == 0
    assert cli.main(["revoke-live"]) == 0 and cli.main(["gate-status"]) == 1
    assert "approved by ops" in capsys.readouterr().out
    assert cli.main(["testnet-check"]) == 1, "testnet-check refuses production"


def test_evidence_file_verify_import_and_approve(tmp_path, monkeypatch, capsys):
    """The GitHub artifact path: signed file -> verify-evidence -> import-evidence on the live host -> approve-live."""
    fake = FakeBinance()
    rep = _validator(fake, []).run("SOLUSDT", order_smoke=True, qty=Decimal("0.5"), max_notional=100)
    ev = gate.sign_evidence(gate.build_evidence(rep), SECRET)
    path = tmp_path / "testnet-evidence.json"
    digest = gate.write_evidence_file(ev, str(path))
    monkeypatch.setenv("IMPERION_STORAGE_DIR", str(tmp_path / "live"))
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("TESTNET_EVIDENCE_SECRET", SECRET)
    assert cli.main(["verify-evidence", str(path)]) == 0
    assert f"EVIDENCE_SHA256={digest}" in capsys.readouterr().out
    assert cli.main(["verify-evidence", str(path), "--expect-sha256", "0" * 64]) == 1
    assert cli.main(["import-evidence", str(path)]) == 0
    assert cli.main(["approve-live", "--operator", "ops", "--confirm", gate.APPROVAL_PHRASE]) == 0
    assert cli.main(["gate-status"]) == 0
    assert cli.main(["import-evidence", str(path)]) == 0 and cli.main(["gate-status"]) == 0, "re-importing the same file keeps the approval"
    bad = tmp_path / "tampered.json"
    bad.write_text(path.read_text().replace('"SOLUSDT"', '"ETHUSDT"'))
    capsys.readouterr()
    assert cli.main(["verify-evidence", str(bad)]) == 1 and "SIGNATURE_INVALID" in capsys.readouterr().out
    assert cli.main(["import-evidence", str(bad)]) == 1 and cli.main(["gate-status"]) == 0, "a rejected file never replaces evidence"
    assert cli.main(["verify-evidence", str(tmp_path / "missing.json")]) == 1
    monkeypatch.setenv("TESTNET_EVIDENCE_SECRET", OTHER_SECRET)
    assert cli.main(["verify-evidence", str(path)]) == 1 and cli.main(["gate-status"]) == 1, "another key on the live host closes the gate"


def test_testnet_check_writes_signed_evidence_and_needs_a_secret_for_orders(tmp_path, monkeypatch, capsys):
    import imperion.testnet as tn
    fake = FakeBinance()
    env = {"MODE": "testnet", "BINANCE_API_KEY": fake.api_key, "BINANCE_API_SECRET": fake.api_secret, "IMPERION_STORAGE_DIR": str(tmp_path),
           "TESTNET_ORDER_SMOKE": "true", "TESTNET_ONLY_ACK": tn.TESTNET_ACK, "TESTNET_SYMBOL": "SOLUSDT", "TESTNET_ORDER_QTY": "0.5",
           "TESTNET_EVIDENCE_OUTPUT": str(tmp_path / "out" / "ev.json")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TESTNET_EVIDENCE_SECRET", raising=False)
    assert tn.main() == 1 and "TESTNET_EVIDENCE_SECRET" in capsys.readouterr().err
    assert not _orders(fake)
    monkeypatch.setenv("TESTNET_EVIDENCE_SECRET", SECRET)
    real_rest = tn.RestClient
    monkeypatch.setattr(tn, "RestClient", lambda *a, **k: real_rest(*a, transport=fake.transport, **k))
    monkeypatch.setattr(tn, "ws_handshake", lambda url, receive_message: {"e": "aggTrade"} if receive_message else None)
    assert tn.main() == 0, capsys.readouterr()
    out = capsys.readouterr().out
    ev = gate.read_evidence_file(str(tmp_path / "out" / "ev.json"))
    assert f"EVIDENCE_SHA256={gate.evidence_hash(ev)}" in out
    assert not gate.verify_evidence(ev, SECRET, 30)
    assert gate.evidence_hash(Database(str(tmp_path / "imperion.db")).kv_get(gate.EVIDENCE_KEY)) == gate.evidence_hash(ev)


def test_release_workflow_publishes_only_with_verified_signed_testnet_evidence():
    tn = yaml.safe_load((ROOT / ".github/workflows/testnet.yml").open(encoding="utf-8"))
    rel = yaml.safe_load((ROOT / ".github/workflows/release.yml").open(encoding="utf-8"))
    on = tn[True] if True in tn else tn["on"]
    assert "evidence_sha256" in on["workflow_call"]["outputs"]
    job = tn["jobs"]["testnet"]
    assert job["env"]["TESTNET_ORDER_SMOKE"] == "true" and "TESTNET_EVIDENCE_SECRET" in job["env"]
    runs = " ".join(str(st.get("run", "")) for st in job["steps"])
    assert "testnet-check" in runs and "verify-evidence" in runs
    upload = [st for st in job["steps"] if str(st.get("uses", "")).startswith("actions/upload-artifact")][0]
    assert upload["with"]["name"] == "testnet-evidence" and upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == 30
    assert rel["jobs"]["testnet"]["uses"] == "./.github/workflows/testnet.yml"
    pub = rel["jobs"]["publish"]
    assert {"ci", "testnet"} <= set(pub["needs"]) and pub["environment"] == "production"
    steps = pub["steps"]
    dl = [i for i, st in enumerate(steps) if str(st.get("uses", "")).startswith("actions/download-artifact")]
    vf = [i for i, st in enumerate(steps) if "verify-evidence" in str(st.get("run", ""))]
    cr = [i for i, st in enumerate(steps) if "gh release create" in str(st.get("run", ""))]
    assert dl and vf and cr and dl[0] < vf[0] < cr[0]
    assert "--expect-sha256" in steps[vf[0]]["run"] and "needs.testnet.outputs.evidence_sha256" in steps[vf[0]]["run"]
    assert "dist/*.json" in steps[cr[0]]["run"]


def test_rate_limit_telemetry_and_alerts():
    engine, fake = make_engine()
    fake.inject("GET", "/fapi/v1/ticker/24hr", times=1, status=429, code=-1003, msg="Too many requests")
    with pytest.raises(Exception):
        engine.rest.ticker_24h()
    st = engine.guard.stats()
    assert st["count_429"] == 1 and st["requests"] > 0 and st["max_weight"] >= 10
    engine._watch_rate_limits()
    assert any(e["kind"] == "rate_limit" and e["level"] == "WARNING" for e in engine.db.events_since(0))
    engine.guard.note_418(120)
    engine._watch_rate_limits()
    assert any(e["kind"] == "rate_limit" and e["level"] == "CRITICAL" for e in engine.db.events_since(0))
    assert engine.status()["rate_limit"]["count_418"] == 1
    engine.stop()
