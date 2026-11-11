"""Shadow trades: every executable plan is tracked to its hypothetical outcome (stop / TP1 / TP2 / expiry).

This gives the learning layer many more labelled samples than the handful of live trades a small account can take,
and it measures setups the risk gate or the AI blocked. Outcomes are conservative: if stop and target fall in the
same candle the stop is assumed to hit first. Each row is tagged with the core that produced it; learning only uses
rows of the current core, so data recorded by an older engine never trains this one.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config import CORE_VERSION, STYLES
from ..db import Database
from ..models import Candle
from ..util import now_ms, tf_ms

log = logging.getLogger("imperion.shadow")

RESOLVE_TF = {"scalp": "1m", "day": "5m", "swing": "1h"}     # 1000 candles cover each style's maximum holding time
TP1_SHARE = 0.5


def simulate_outcome(side: str, entry: float, stop: float, tp1: float, tp2: float, candles: Sequence[Candle],
                     cost_r: float = 0.0) -> Optional[Dict[str, Any]]:
    """Walk candles after entry. Returns None while unresolved, else {status, r, bars} (bars = candles until the exit)."""
    long_ = side == "LONG"
    risk = abs(entry - stop)
    if risk <= 0:
        return {"status": "INVALID", "r": 0.0, "bars": 0}
    tp1_r = abs(tp1 - entry) / risk
    tp2_r = abs(tp2 - entry) / risk
    stage = 0           # 0 = full position, 1 = TP1 taken (stop at break-even)
    cur_stop = stop
    for bars, c in enumerate(candles, 1):
        stop_hit = c.l <= cur_stop if long_ else c.h >= cur_stop
        if stage == 0:
            if stop_hit:
                return {"status": "STOP", "r": -1.0 - cost_r, "bars": bars}
            if (c.h >= tp1) if long_ else (c.l <= tp1):
                stage, cur_stop = 1, entry      # TP1 and TP2 in one candle: only TP1 is credited (conservative)
                continue
        else:
            if stop_hit:
                return {"status": "BE_AFTER_TP1", "r": TP1_SHARE * tp1_r - cost_r, "bars": bars}
            if (c.h >= tp2) if long_ else (c.l <= tp2):
                return {"status": "TARGET", "r": TP1_SHARE * tp1_r + (1 - TP1_SHARE) * tp2_r - cost_r, "bars": bars}
    return None


def mark_to_market(side: str, entry: float, stop: float, last_close: float, stage_tp1: bool, tp1: float, cost_r: float) -> float:
    risk = abs(entry - stop)
    move = (last_close - entry) if side == "LONG" else (entry - last_close)
    if stage_tp1:
        return TP1_SHARE * abs(tp1 - entry) / risk + (1 - TP1_SHARE) * move / risk - cost_r
    return move / risk - cost_r


class ShadowTracker:
    def __init__(self, db: Database, fetch: Optional[Callable[[str, str, int, int], List[list]]] = None):
        self.db, self.fetch = db, fetch

    def record(self, decision_id: Optional[int], symbol: str, style: str, side: str, entry: float, stop: float,
               tp1: float, tp2: float, snapshot: Dict[str, Dict[str, Any]], source: str, now: Optional[int] = None,
               meta: Optional[Dict[str, Any]] = None) -> int:
        """`snapshot` = Analysis.snapshot() ({agent: {"d", "s"}}); `meta` = decision context (playbook, grade, regime, ...)."""
        now = now if now is not None else now_ms()
        meta = {"core": CORE_VERSION, **(meta or {})}
        horizon = now + STYLES[style].max_hold_minutes * 60_000
        cur = self.db.execute(
            "INSERT INTO shadows(ts,symbol,style,side,entry,stop,tp1,tp2,horizon_ts,status,evidence_json,decision_id,source,meta_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)",
            (now, symbol, style, side, entry, stop, tp1, tp2, horizon, json.dumps(snapshot), decision_id, source,
             json.dumps(meta, default=str)),
        )
        return int(cur.lastrowid)

    def resolve_open(self, cost_pct: float, now: Optional[int] = None, limit: int = 40) -> int:
        """Resolve open shadows via REST klines from the entry time. Returns number resolved."""
        if self.fetch is None:
            return 0
        now = now if now is not None else now_ms()
        done = 0
        for s in self.db.query("SELECT * FROM shadows WHERE status='OPEN' AND ts < ? ORDER BY ts LIMIT ?", (now - 60_000, limit)):
            tf = RESOLVE_TF[s["style"]]
            start = s["ts"] - s["ts"] % tf_ms(tf) + tf_ms(tf)          # first candle fully after the signal
            try:
                rows = self.fetch(s["symbol"], tf, start, 1000)
            except Exception as exc:
                log.debug("shadow fetch failed %s: %s", s["symbol"], exc)
                continue
            candles = [Candle.from_rest(r) for r in rows if int(r[6]) < now]
            risk = abs(s["entry"] - s["stop"])
            cost_r = (cost_pct / 100 * s["entry"]) / risk if risk else 0.0
            res = simulate_outcome(s["side"], s["entry"], s["stop"], s["tp1"], s["tp2"], candles, cost_r)
            if res is None and now >= s["horizon_ts"] and candles:
                stage = any((c.h >= s["tp1"]) if s["side"] == "LONG" else (c.l <= s["tp1"]) for c in candles)
                res = {"status": "EXPIRED", "r": mark_to_market(s["side"], s["entry"], s["stop"], candles[-1].c, stage, s["tp1"], cost_r)}
            if res is not None:
                self.db.execute("UPDATE shadows SET status=?, r_result=?, resolved_ts=? WHERE id=?", (res["status"], round(res["r"], 4), now, s["id"]))
                done += 1
        return done

    def resolved(self, since_ms: int = 0, core: Optional[str] = CORE_VERSION) -> List[Dict[str, Any]]:
        """Resolved shadows, oldest first. `core=None` returns rows of every engine version."""
        rows = self.db.query("SELECT * FROM shadows WHERE status NOT IN ('OPEN','INVALID') AND ts>=? ORDER BY ts", (since_ms,))
        out = []
        for r in rows:
            r["evidence"] = json.loads(r["evidence_json"] or "{}")
            r["meta"] = json.loads(r.get("meta_json") or "{}")
            if core is None or r["meta"].get("core") == core:
                out.append(r)
        return out
