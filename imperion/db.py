"""SQLite persistence (stdlib only). One shared connection guarded by a lock; WAL mode."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .util import now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_ts INTEGER);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, kind TEXT, message TEXT, data_json TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, style TEXT, trigger_tf TEXT,
  direction TEXT, approved INTEGER, status TEXT, net_score REAL, quality REAL,
  reasons_json TEXT, evidence_json TEXT, setup_json TEXT, weights_version INTEGER, hint TEXT, data_ok INTEGER);
CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY, ts_open INTEGER, ts_close INTEGER, symbol TEXT, style TEXT, side TEXT,
  entry REAL, qty REAL, stop REAL, tp1 REAL, tp2 REAL, risk_usdt REAL, status TEXT,
  exit_price REAL, pnl_usdt REAL, fees_usdt REAL, r_multiple REAL, exit_reason TEXT,
  mode TEXT, decision_id INTEGER, meta_json TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(status);
CREATE TABLE IF NOT EXISTS shadows (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, style TEXT, side TEXT,
  entry REAL, stop REAL, tp1 REAL, tp2 REAL, horizon_ts INTEGER, status TEXT,
  r_result REAL, resolved_ts INTEGER, evidence_json TEXT, decision_id INTEGER, source TEXT);
CREATE INDEX IF NOT EXISTS ix_shadows_status ON shadows(status);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER, equity REAL, available REAL, unrealized REAL, source TEXT);
CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity(ts);
CREATE TABLE IF NOT EXISTS webhook_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, ts INTEGER, payload_json TEXT,
  status TEXT, attempts INTEGER DEFAULT 0, result_json TEXT, processed_ts INTEGER);
CREATE TABLE IF NOT EXISTS ai_weights (
  version INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, status TEXT, weights_json TEXT,
  metrics_json TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS reports (day TEXT PRIMARY KEY, ts INTEGER, ok INTEGER, detail TEXT);
"""


def _clean(obj: Any) -> Any:
    """Recursively replace NaN/inf (not valid JSON) with None."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_clean(v) for v in obj]
    return obj


def _j(obj: Any) -> str:
    return json.dumps(_clean(obj), default=str, separators=(",", ":"), allow_nan=False)


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Additive, idempotent schema upgrades for databases created by older versions."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(shadows)").fetchall()}
        if "meta_json" not in cols:          # v9: decision context (regime, session, quality) for the AI context filter
            self._conn.execute("ALTER TABLE shadows ADD COLUMN meta_json TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- primitives ----------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- key/value -----------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value,updated_ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
            (key, _j(value), now_ms()),
        )

    # ---- events --------------------------------------------------------
    def event(self, level: str, kind: str, message: str, data: Any = None) -> None:
        self.execute(
            "INSERT INTO events(ts,level,kind,message,data_json) VALUES(?,?,?,?,?)",
            (now_ms(), level.upper(), kind, message[:2000], _j(data)),
        )

    def events_since(self, ts_ms: int, levels: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM events WHERE ts>=? ORDER BY ts", (ts_ms,))
        if levels:
            wanted = {x.upper() for x in levels}
            rows = [r for r in rows if r["level"] in wanted]
        return rows

    # ---- decisions -----------------------------------------------------
    def add_decision(self, d: Dict[str, Any]) -> int:
        cur = self.execute(
            "INSERT INTO decisions(ts,symbol,style,trigger_tf,direction,approved,status,net_score,quality,"
            "reasons_json,evidence_json,setup_json,weights_version,hint,data_ok) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d.get("ts", now_ms()), d["symbol"], d["style"], d.get("trigger_tf"), d.get("direction", "NO TRADE"),
                1 if d.get("approved") else 0, d.get("status", ""), d.get("net_score"), d.get("quality"),
                _j(d.get("reasons", [])), _j(d.get("evidence", {})), _j(d.get("setup")),
                d.get("weights_version"), d.get("hint"), 1 if d.get("data_ok") else 0,
            ),
        )
        return int(cur.lastrowid)

    def decisions_since(self, ts_ms: int) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM decisions WHERE ts>=? ORDER BY ts", (ts_ms,))

    # ---- trades --------------------------------------------------------
    def upsert_trade(self, t: Dict[str, Any]) -> None:
        cols = ["id", "ts_open", "ts_close", "symbol", "style", "side", "entry", "qty", "stop", "tp1", "tp2",
                "risk_usdt", "status", "exit_price", "pnl_usdt", "fees_usdt", "r_multiple", "exit_reason",
                "mode", "decision_id", "meta_json"]
        row = dict(t)
        if "meta" in row:
            row["meta_json"] = _j(row.pop("meta"))
        with self._lock:                 # read-merge-write must be atomic (guardian + decision + webhook threads)
            existing = self.one("SELECT * FROM trades WHERE id=?", (row["id"],))
            if existing:
                merged = {**existing, **{k: v for k, v in row.items() if k in cols}}
            else:
                merged = {c: row.get(c) for c in cols}
            self.execute(
                f"INSERT OR REPLACE INTO trades({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                [merged.get(c) for c in cols],
            )

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT * FROM trades WHERE id=?", (trade_id,))
        if row and row.get("meta_json"):
            row["meta"] = json.loads(row["meta_json"]) or {}
        return row

    def open_trades(self) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM trades WHERE status IN ('OPEN','PENDING','ENTRY_UNKNOWN','CLOSING') ORDER BY ts_open")
        for r in rows:
            r["meta"] = json.loads(r["meta_json"]) if r.get("meta_json") else {}
        return rows

    def closed_trades_since(self, ts_ms: int) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM trades WHERE status='CLOSED' AND ts_close>=? ORDER BY ts_close", (ts_ms,))
        for r in rows:
            r["meta"] = json.loads(r["meta_json"]) if r.get("meta_json") else {}
        return rows

    def trades_opened_since(self, ts_ms: int) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM trades WHERE ts_open>=? ORDER BY ts_open", (ts_ms,))

    # ---- equity --------------------------------------------------------
    def add_equity(self, equity: float, available: float, unrealized: float, source: str) -> None:
        self.execute("INSERT INTO equity VALUES(?,?,?,?,?)", (now_ms(), equity, available, unrealized, source))

    def equity_since(self, ts_ms: int) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM equity WHERE ts>=? ORDER BY ts", (ts_ms,))

    # ---- webhook queue -------------------------------------------------
    def enqueue_webhook(self, key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            row = self.one("SELECT id FROM webhook_queue WHERE key=?", (key,))
            if row:
                return {"id": row["id"], "duplicate": True}
            cur = self.execute(
                "INSERT INTO webhook_queue(key,ts,payload_json,status) VALUES(?,?,?,'PENDING')",
                (key, now_ms(), _j(payload)),
            )
            return {"id": int(cur.lastrowid), "duplicate": False}

    def pending_webhooks(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM webhook_queue WHERE status='PENDING' ORDER BY id LIMIT ?", (limit,))

    def finish_webhook(self, wid: int, status: str, result: Any) -> None:
        self.execute(
            "UPDATE webhook_queue SET status=?, result_json=?, processed_ts=?, attempts=attempts+1 WHERE id=?",
            (status, _j(result), now_ms(), wid),
        )

    # ---- AI weight versions -------------------------------------------
    def add_weights(self, weights: Dict[str, float], status: str, metrics: Any = None, note: str = "") -> int:
        cur = self.execute(
            "INSERT INTO ai_weights(ts,status,weights_json,metrics_json,note) VALUES(?,?,?,?,?)",
            (now_ms(), status, _j(weights), _j(metrics), note),
        )
        return int(cur.lastrowid)

    def set_weights_status(self, version: int, status: str) -> None:
        self.execute("UPDATE ai_weights SET status=? WHERE version=?", (status, version))

    def weights_by_status(self, status: str) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM ai_weights WHERE status=? ORDER BY version DESC", (status,))
        for r in rows:
            r["weights"] = json.loads(r["weights_json"])
            r["metrics"] = json.loads(r["metrics_json"]) if r.get("metrics_json") else None
        return rows

    # ---- reports -------------------------------------------------------
    def report_done(self, day: str) -> bool:
        row = self.one("SELECT ok FROM reports WHERE day=?", (day,))
        return bool(row and row["ok"])

    def mark_report(self, day: str, ok: bool, detail: str) -> None:
        self.execute("INSERT OR REPLACE INTO reports VALUES(?,?,?,?)", (day, now_ms(), 1 if ok else 0, detail[:500]))

    # ---- housekeeping --------------------------------------------------
    def prune(self, now: Optional[int] = None) -> Dict[str, int]:
        """Delete old rows. Trades, shadows and reports are kept (they are the learning / audit record)."""
        now = now if now is not None else now_ms()
        day = 86_400_000
        out: Dict[str, int] = {}
        for table, keep_days, extra in (
            ("decisions", 14, " AND approved=0"),
            ("events", 45, " AND level!='CRITICAL'"),
            ("equity", 90, ""),
            ("webhook_queue", 14, " AND status!='PENDING'"),
        ):
            cur = self.execute(f"DELETE FROM {table} WHERE ts < ?{extra}", (now - keep_days * day,))
            out[table] = cur.rowcount
        return out
