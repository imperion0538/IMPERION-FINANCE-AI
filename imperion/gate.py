"""Testnet -> Live gate. Real-money orders need, on the same database:

  1. signed testnet evidence  a passed `testnet-check` on Binance Futures Demo that included every required check:
                              clock, market data, signed REST, ONE-WAY position mode, both WebSockets, the bounded
                              ORDER LIFECYCLE (entry, exchange-confirmed stop, reduce-only close, cleanup) and the
                              EXCHANGE LIQUIDATION check (real liquidationPrice vs stop and vs our own model).
                              The evidence is signed with HMAC-SHA256 (TESTNET_EVIDENCE_SECRET); the signature is
                              verified when the evidence is recorded or imported AND every time the gate is evaluated,
                              so an edited database row or file closes the gate. It must be younger than
                              LIVE_GATE_MAX_AGE_DAYS and come from this major version.
  2. manual approval          `approve-live --operator NAME --confirm I_APPROVE_LIVE_TRADING`, bound to that exact
                              evidence (SHA-256 of the signed evidence). New evidence invalidates an old approval.

Threat model of the signature: a shared secret proves that the evidence was produced by a validator holding the
secret (your GitHub Actions run or your own `testnet-check`) and that nobody edited it since. It cannot protect against
someone who already holds the secret; keep it only in GitHub secrets and the live host's environment.
The gate only ever blocks; it never opens positions or changes risk limits.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import VERSION, Settings
from .db import Database
from .util import iso, now_ms

EVIDENCE_KEY = "gate.testnet_evidence"
APPROVAL_KEY = "gate.live_approval"
APPROVAL_PHRASE = "I_APPROVE_LIVE_TRADING"
DAY_MS = 86_400_000
EVIDENCE_SCHEMA = 2
SIG_ALG = "HMAC-SHA256"
MIN_SECRET_LEN = 32
FUTURE_SKEW_MS = 5 * 60_000
# Every one of these must have passed in the same run before live trading can be approved.
REQUIRED_CHECKS = ("clock", "market_data", "signed_rest", "position_mode", "market_websocket", "user_stream_handshake",
                   "order_lifecycle", "exchange_liquidation")
_SIGNED_FIELDS_EXCLUDED = ("signature",)


def _major(v: str) -> str:
    return str(v).split(".")[0]


def _canonical(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str).encode()


def evidence_hash(ev: Dict[str, Any]) -> str:
    """SHA-256 of the complete (signed) evidence: the value a manual approval is bound to."""
    return hashlib.sha256(_canonical(ev)).hexdigest()


def key_id(secret: str) -> str:
    """Public identifier of a signing key (an HMAC of a constant, not a hash of the secret)."""
    return hmac.new(secret.encode(), b"imperion-testnet-evidence-key-id", hashlib.sha256).hexdigest()[:12]


def _signature(ev: Dict[str, Any], secret: str) -> str:
    body = {k: v for k, v in ev.items() if k not in _SIGNED_FIELDS_EXCLUDED}
    return hmac.new(secret.encode(), _canonical(body), hashlib.sha256).hexdigest()


def sign_evidence(ev: Dict[str, Any], secret: str) -> Dict[str, Any]:
    if len(secret or "") < MIN_SECRET_LEN:
        raise ValueError(f"TESTNET_EVIDENCE_SECRET must be at least {MIN_SECRET_LEN} characters")
    out = {k: v for k, v in ev.items() if k not in _SIGNED_FIELDS_EXCLUDED}
    out.update(sig_alg=SIG_ALG, key_id=key_id(secret))
    out["signature"] = _signature(out, secret)
    return out


def signature_problems(ev: Dict[str, Any], secret: str) -> List[str]:
    if len(secret or "") < MIN_SECRET_LEN:
        return [f"TESTNET_EVIDENCE_SECRET_MISSING (set TESTNET_EVIDENCE_SECRET, >= {MIN_SECRET_LEN} chars, the same value the validator used)"]
    sig = str(ev.get("signature") or "")
    if not sig or ev.get("sig_alg") != SIG_ALG:
        return ["TESTNET_EVIDENCE_UNSIGNED (rerun testnet-check with TESTNET_EVIDENCE_SECRET set)"]
    if ev.get("key_id") != key_id(secret):
        return ["TESTNET_EVIDENCE_SIGNED_WITH_OTHER_KEY (TESTNET_EVIDENCE_SECRET differs from the validator's)"]
    if not hmac.compare_digest(sig, _signature(ev, secret)):
        return ["TESTNET_EVIDENCE_SIGNATURE_INVALID (the evidence was modified after signing)"]
    return []


def build_evidence(report: Dict[str, Any]) -> Dict[str, Any]:
    """Unsigned evidence from a PASSED validator report (only what the gate needs; no keys, no balances)."""
    return {"schema": EVIDENCE_SCHEMA, "run_id": uuid.uuid4().hex, "ts": int(report.get("ts") or now_ms()),
            "version": str(report.get("version") or VERSION), "symbol": report.get("symbol"), "host": report.get("host"),
            "order_lifecycle": bool(report.get("order_lifecycle")), "position_mode": report.get("position_mode"),
            "liquidation": report.get("liquidation"), "source": report.get("source") or "testnet-check",
            "checks": sorted(c["name"] for c in report.get("checks", []) if c.get("ok"))}


def verify_evidence(ev: Optional[Dict[str, Any]], secret: str, max_age_days: int, now: Optional[int] = None) -> List[str]:
    """Every reason this evidence cannot open the live gate (empty list = valid)."""
    now = now if now is not None else now_ms()
    if not ev:
        return ["NO_TESTNET_EVIDENCE (run `python -m imperion testnet-check` with the order smoke test, or `import-evidence FILE`)"]
    if not isinstance(ev, dict) or ev.get("schema") != EVIDENCE_SCHEMA:
        return [f"TESTNET_EVIDENCE_OLD_FORMAT (schema {ev.get('schema') if isinstance(ev, dict) else '?'} != {EVIDENCE_SCHEMA}: rerun testnet-check)"]
    out = signature_problems(ev, secret)
    if not ev.get("order_lifecycle"):
        out.append("TESTNET_EVIDENCE_WITHOUT_ORDER_LIFECYCLE (rerun testnet-check with TESTNET_ORDER_SMOKE=true)")
    missing = [c for c in REQUIRED_CHECKS if c not in set(ev.get("checks") or [])]
    if missing:
        out.append("TESTNET_EVIDENCE_INCOMPLETE (missing: " + ", ".join(missing) + ")")
    if ev.get("position_mode") != "ONE_WAY":
        out.append(f"TESTNET_EVIDENCE_POSITION_MODE ({ev.get('position_mode')!r}; One-way mode is required)")
    liq = ev.get("liquidation") or {}
    if not liq.get("ok") or not float(liq.get("exchange_liq_price") or 0) > 0:
        out.append("TESTNET_EVIDENCE_NO_EXCHANGE_LIQUIDATION (the real liquidationPrice check did not pass)")
    ts = int(ev.get("ts") or 0)
    if ts > now + FUTURE_SKEW_MS:
        out.append("TESTNET_EVIDENCE_FUTURE_DATED")
    age_days = (now - ts) / DAY_MS
    if age_days > max_age_days:
        out.append(f"TESTNET_EVIDENCE_EXPIRED ({age_days:.0f} days > {max_age_days})")
    if _major(ev.get("version", "")) != _major(VERSION):
        out.append(f"TESTNET_EVIDENCE_FROM_OTHER_VERSION ({ev.get('version')} vs {VERSION})")
    return out


def record_testnet_evidence(db: Database, report: Dict[str, Any], secret: str) -> Optional[Dict[str, Any]]:
    """Sign and store a PASSED validation report. Failed reports, or a missing secret, record nothing."""
    if not report.get("passed"):
        db.event("WARNING", "gate_evidence", "testnet validation failed; no gate evidence recorded",
                 {"failed": [c["name"] for c in report.get("checks", []) if not c.get("ok")]})
        return None
    if len(secret or "") < MIN_SECRET_LEN:
        db.event("WARNING", "gate_evidence", "testnet validation passed but TESTNET_EVIDENCE_SECRET is not set: no evidence recorded")
        return None
    ev = sign_evidence(build_evidence(report), secret)
    db.kv_set(EVIDENCE_KEY, ev)
    db.event("INFO", "gate_evidence", f"signed testnet evidence recorded (order lifecycle: {ev['order_lifecycle']}) {evidence_hash(ev)[:12]}")
    return ev


def write_evidence_file(ev: Dict[str, Any], path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence_hash(ev)


def read_evidence_file(path: str) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("evidence file must contain a JSON object")
    return data


def import_testnet_evidence(db: Database, path: str, secret: str, max_age_days: int, now: Optional[int] = None) -> Dict[str, Any]:
    """Verify a signed evidence file (e.g. the GitHub Actions artifact) and store it. Raises with every problem."""
    ev = read_evidence_file(path)
    problems = verify_evidence(ev, secret, max_age_days, now)
    if problems:
        raise ValueError("evidence rejected: " + "; ".join(problems))
    current = db.kv_get(EVIDENCE_KEY)
    if current and evidence_hash(current) == evidence_hash(ev):
        return ev                                    # identical evidence: an existing approval stays valid
    db.kv_set(EVIDENCE_KEY, ev)
    db.event("INFO", "gate_evidence", f"signed testnet evidence imported from file {evidence_hash(ev)[:12]}")
    return ev


def approve_live(db: Database, operator: str, confirm: str, max_age_days: int, secret: str, now: Optional[int] = None) -> Dict[str, Any]:
    now = now if now is not None else now_ms()
    if confirm != APPROVAL_PHRASE:
        raise ValueError(f"approval needs --confirm {APPROVAL_PHRASE}")
    if not operator or not operator.strip():
        raise ValueError("approval needs --operator NAME")
    ev = db.kv_get(EVIDENCE_KEY)
    problems = verify_evidence(ev, secret, max_age_days, now)
    if problems:
        raise ValueError("cannot approve live trading: " + "; ".join(problems))
    approval = {"ts": now, "operator": operator.strip()[:80], "version": VERSION, "evidence_hash": evidence_hash(ev),
                "evidence_key_id": ev.get("key_id"), "revoked": False}
    db.kv_set(APPROVAL_KEY, approval)
    db.event("WARNING", "gate_approval", f"LIVE trading approved by {approval['operator']} (evidence {approval['evidence_hash'][:12]})")
    return approval


def revoke_live(db: Database, by: str = "operator") -> None:
    approval = db.kv_get(APPROVAL_KEY) or {}
    db.kv_set(APPROVAL_KEY, {**approval, "revoked": True, "revoked_by": by, "revoked_ts": now_ms()})
    db.event("WARNING", "gate_revoked", f"LIVE trading approval revoked by {by}")


def live_gate_status(db: Database, cfg: Settings, now: Optional[int] = None) -> Dict[str, Any]:
    now = now if now is not None else now_ms()
    ev, ap = db.kv_get(EVIDENCE_KEY), db.kv_get(APPROVAL_KEY)
    reasons = verify_evidence(ev, cfg.testnet_evidence_secret, cfg.live_gate_max_age_days, now)
    if not ap or ap.get("revoked"):
        reasons.append(f"NO_MANUAL_APPROVAL (run `python -m imperion approve-live --operator NAME --confirm {APPROVAL_PHRASE}`)")
    else:
        if ev and ap.get("evidence_hash") != evidence_hash(ev):
            reasons.append("APPROVAL_FOR_OTHER_EVIDENCE (new testnet evidence was recorded: approve again)")
        if _major(ap.get("version", "")) != _major(VERSION):
            reasons.append(f"APPROVAL_FROM_OTHER_VERSION ({ap.get('version')} vs {VERSION})")
    evidence = None
    if isinstance(ev, dict):
        evidence = {k: ev.get(k) for k in ("ts", "version", "symbol", "order_lifecycle", "position_mode", "key_id", "source")}
        evidence.update(at=iso(int(ev.get("ts") or 0)), hash=evidence_hash(ev)[:16],
                        signed=not signature_problems(ev, cfg.testnet_evidence_secret),
                        liquidation=(ev.get("liquidation") or {}).get("exchange_liq_price"))
    return {"applies": cfg.mode == "live", "ok": not reasons, "reasons": reasons, "evidence": evidence,
            "approval": {k: ap.get(k) for k in ("ts", "operator", "version", "revoked")} if ap else None}
