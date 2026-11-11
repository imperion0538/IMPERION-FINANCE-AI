"""Trade lifecycle: entry -> exchange-side stop -> take-profits -> break-even -> reconciliation.

Hard rules implemented here:
  1. A position must never exist without an exchange-side stop. If the stop cannot be placed
     (after retries) the position is closed at market immediately.
  2. The guardian re-creates a missing stop on every cycle; if that fails it flattens.
  3. Orphan protective orders are cancelled when a position is gone.
  4. Unknown ("foreign") positions are never touched, only counted and reported.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from ..config import STYLES, Settings
from ..db import Database
from ..exchange.rest import BinanceAPIError, NetworkError, RateLimited
from ..risk.liquidation import stop_beyond_liquidation
from ..risk.manager import RiskManager
from ..models import TradeSetup
from ..util import now_ms
from .base import ExecutionError, Executor

log = logging.getLogger("imperion.lifecycle")

BE_FEE_BUFFER = 0.0012      # stop moved to entry +/- 0.12% (covers round-trip fees)
MAX_SLIPPAGE_RISK_MULT = 1.6
ROUTINE_CLOSE_REASONS = {"TIME_STOP", "ADMIN_FLATTEN"}
EXPOSURE_HALT_AFTER_S = 60.0   # positions unreadable for this long -> halt (until then entries are only blocked)
PENDING_GRACE_MS = 30_000
TRAIL_MIN_STEP_ATR = 0.25      # do not re-place the stop for improvements smaller than this (order-rate discipline)
FILL_TIME_SLACK_MS = 5_000  # exchange fill timestamps vs local clock (the style cooldown keeps older fills out)
LIQ_MARGIN = 0.002          # liquidation within 0.2 % of the stop already counts as "beyond"
FINALIZE_RETRIES = 12     # x 4 s guardian cycle: wait up to ~48 s for exit fills to appear in userTrades


def new_trade_id() -> str:
    return "t" + format(now_ms() // 1000, "x") + secrets.token_hex(2)


class Lifecycle:
    def __init__(self, cfg: Settings, db: Database, risk: RiskManager, execu: Executor,
                 alert: Optional[Callable[[str, str], None]] = None):
        self.cfg, self.db, self.risk, self.exec = cfg, db, risk, execu
        self.alert = alert or (lambda level, msg: None)
        self._lock = threading.RLock()
        self._finalize_attempts: Dict[str, int] = {}
        self._foreign_reported: set = set()
        self._order_errors = 0
        self._pos_cache: tuple = (0.0, {})
        self._exposure_fail_since = 0.0

    # ------------------------------------------------------------------
    def open_trades(self) -> List[Dict[str, Any]]:
        return self.db.open_trades()

    def exposure(self) -> List[Dict[str, Any]]:
        """Open DB trades + foreign exchange positions (counted conservatively)."""
        trades = self.open_trades()
        known = {t["symbol"] for t in trades}
        try:
            ts, cached = self._pos_cache
            if time.time() - ts > 3.0:
                cached = self.exec.positions()
                self._pos_cache = (time.time(), cached)
            self._exposure_fail_since = 0.0
            foreign = [p for s, p in cached.items() if s not in known]
        except Exception as exc:
            # Unknown exchange exposure is never read as "no positions": the marker row below blocks every new entry
            # (risk.precheck -> EXPOSURE_UNKNOWN). A short blip only blocks; a persistent outage halts trading.
            now = time.time()
            if not self._exposure_fail_since:
                self._exposure_fail_since = now
                self.db.event("ERROR", "exposure_unknown", f"Cannot read exchange positions; new entries blocked: {exc}")
            elif now - self._exposure_fail_since >= EXPOSURE_HALT_AFTER_S and not self.risk.halted():
                self.risk.halt(f"EXCHANGE POSITION QUERY FAILING for {now - self._exposure_fail_since:.0f}s: exposure unknown ({exc})", "lifecycle")
            trades.append({"symbol": "__EXPOSURE_UNKNOWN__", "side": "UNKNOWN", "risk_usdt": self.cfg.trading_capital_usdt,
                           "meta": {"foreign": True, "unknown": True}, "style": "unknown"})
            return trades
        per_trade = self.cfg.trading_capital_usdt * self.cfg.risk_per_trade_pct / 100
        for p in foreign:
            trades.append({"symbol": p.symbol, "side": p.side, "risk_usdt": per_trade, "meta": {"foreign": True}, "style": "foreign"})
            if p.symbol not in self._foreign_reported:
                self._foreign_reported.add(p.symbol)
                fresh_db = (self.db.one("SELECT COUNT(*) AS c FROM trades") or {"c": 0})["c"] == 0
                self.db.event("WARNING", "foreign_position", f"Position in {p.symbol} not opened by this system; left untouched", {"qty": p.qty})
                if fresh_db:   # most likely the database was lost (no persistent volume): positions keep their exchange stops but are unmanaged
                    self.alert("CRITICAL", f"Position in {p.symbol} (qty {p.qty}) exists but the trade database is EMPTY. If this is a bot position, "
                                           f"the storage volume was lost: check the stop/TP orders on Binance manually and mount a persistent volume.")
                else:
                    self.alert("WARNING", f"Foreign position detected: {p.symbol} qty {p.qty}")
        return trades

    # ------------------------------------------------------------------
    def open_trade(self, setup: TradeSetup, sizing: Dict[str, Any], decision_id: Optional[int], meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sym, side, qty = setup.symbol, setup.side, sizing["qty"]
        tid = new_trade_id()
        self.db.upsert_trade({
            "id": tid, "ts_open": now_ms(), "symbol": sym, "style": setup.style, "side": side, "entry": setup.entry,
            "qty": float(qty), "stop": setup.stop, "tp1": setup.tp1, "tp2": setup.tp2, "risk_usdt": sizing["risk_usdt"],
            "status": "PENDING", "mode": self.exec.mode, "decision_id": decision_id, "meta": {**(meta or {}), "plan_entry": setup.entry, "atr": setup.atr},
        })
        try:
            self.exec.prepare(sym)
            fill = self.exec.enter(sym, side, qty, tid + "e")
        except (ExecutionError, BinanceAPIError, NetworkError, RateLimited) as exc:
            return self._fail(tid, f"ENTRY_FAILED: {exc}", sym)
        if fill.qty <= 0:
            return self._fail(tid, "ENTRY_NOT_FILLED", sym)
        entry = fill.avg_price or setup.entry
        t = self.db.get_trade(tid)
        meta_d = dict(t.get("meta") or {})
        meta_d["fill_status"] = fill.status
        risk_actual = abs(entry - setup.stop) * float(fill.qty)
        # ts_open stays the moment BEFORE the entry order: the entry fill (and its fee) must fall inside the PnL window
        self.db.upsert_trade({"id": tid, "entry": entry, "qty": float(fill.qty), "status": "OPEN",
                              "risk_usdt": max(risk_actual, 1e-9), "meta": meta_d})

        # 1) protective stop (mandatory)
        if not self._ensure_stop(sym, side, setup.stop, fill.qty, tid):
            self._emergency_close(sym, side, fill.qty, tid, "NO_STOP")
            self._order_errors += 1
            if self._order_errors >= 2:
                self.risk.halt("protective stop could not be placed twice in a row", "lifecycle")
            return self.db.get_trade(tid)
        self._order_errors = 0

        # 1b) the exchange's own liquidation price must lie beyond the stop (a stop past liquidation never fires)
        liq = self._exchange_liq(sym)
        meta_d = dict(self.db.get_trade(tid).get("meta") or {})
        meta_d.update(liq_exchange=liq, liq_planned=sizing.get("liq_price"))
        self.db.upsert_trade({"id": tid, "meta": meta_d})
        if liq and stop_beyond_liquidation(side, setup.stop, liq, LIQ_MARGIN):
            self.db.event("CRITICAL", "stop_beyond_liquidation", f"{sym}: exchange liquidation {liq:.6g} is at/inside the stop {setup.stop:.6g}; closing")
            self._emergency_close(sym, side, fill.qty, tid, "STOP_BEYOND_LIQUIDATION")
            return self.db.get_trade(tid)

        # slippage sanity: if the realised risk blew through the plan, do not keep the trade
        if sizing["planned_risk_usdt"] > 0 and risk_actual > sizing["planned_risk_usdt"] * MAX_SLIPPAGE_RISK_MULT:
            self.db.event("WARNING", "slippage_abort", f"{sym}: realised risk {risk_actual:.2f} > {MAX_SLIPPAGE_RISK_MULT}x plan; closing")
            self._emergency_close(sym, side, fill.qty, tid, "SLIPPAGE_ABORT")
            return self.db.get_trade(tid)

        # 2) take-profits (failures are logged, the guardian retries)
        try:
            if sizing.get("split") and sizing["tp1_qty"] > 0:
                tp1_qty = min(Decimal(str(sizing["tp1_qty"])), fill.qty)
                remainder = fill.qty - tp1_qty
                if tp1_qty > 0:
                    self.exec.place_tp(sym, side, setup.tp1, tp1_qty, tid + "p")
                if remainder > 0:
                    self.exec.place_tp(sym, side, setup.tp2, remainder, tid + "q")
            else:
                self.exec.place_tp(sym, side, setup.tp2, fill.qty, tid + "q")
        except Exception as exc:
            self.db.event("ERROR", "tp_failed", f"{sym}: take-profit placement failed: {exc}")
        try:
            self.risk.register_new_risk(risk_actual, self.risk.trading_equity(self.exec.account().equity))
        except Exception:
            self.risk.register_new_risk(risk_actual, self.cfg.trading_capital_usdt)
        self.db.event("INFO", "trade_open", f"{side} {sym} qty={fill.qty} entry={entry} stop={setup.stop} tp1={setup.tp1} tp2={setup.tp2}", {"id": tid})
        self.alert("INFO", f"OPENED {side} {sym} @ {entry} stop {setup.stop} tp {setup.tp2} risk {risk_actual:.2f} USDT")
        return self.db.get_trade(tid)

    def _fail(self, tid: str, reason: str, sym: str) -> Dict[str, Any]:
        # A failed request is not proof that the exchange rejected the order.
        meta = dict((self.db.get_trade(tid) or {}).get("meta") or {})
        try:
            positions = self.exec.positions()
        except Exception as exc:
            self.db.upsert_trade({"id": tid, "status": "ENTRY_UNKNOWN", "exit_reason": reason,
                                  "meta": {**meta, "reconciliation_required": True, "position_query_error": str(exc)}})
            self.risk.halt(f"ENTRY RESULT UNKNOWN for {sym}; exchange position state unavailable", "lifecycle")
            self.alert("CRITICAL", f"{sym}: entry outcome unknown. New entries halted; reconcile Binance orders/positions.")
            self.db.event("CRITICAL", "entry_unknown_state", f"{sym}: cannot verify entry after error: {exc}")
            return self.db.get_trade(tid)
        pos = positions.get(sym)
        if pos is not None:
            self.db.event("CRITICAL", "entry_unknown_state", f"{sym}: entry reported failure but position exists; flattening ({reason})")
            self.db.upsert_trade({"id": tid, "status": "OPEN", "qty": abs(float(pos.qty)),
                                  "entry": float(pos.entry), "meta": {**meta, "reconciliation_required": True}})
            self._emergency_close(sym, pos.side, Decimal(str(abs(pos.qty))), tid, "ENTRY_STATE_UNKNOWN")
        else:
            self.db.upsert_trade({"id": tid, "status": "FAILED", "exit_reason": reason, "ts_close": now_ms()})
            self.db.event("ERROR", "entry_failed", f"{sym}: {reason}")
        self._order_errors += 1
        if self._order_errors >= 3:
            self.risk.halt(f"{self._order_errors} consecutive order failures", "lifecycle")
        return self.db.get_trade(tid)

    def _exchange_liq(self, sym: str) -> Optional[float]:
        try:
            pos = self.exec.positions().get(sym)
        except Exception as exc:
            log.warning("liquidation price check failed for %s: %s", sym, exc)
            return None
        liq = float(getattr(pos, "liq_price", 0.0) or 0.0) if pos is not None else 0.0
        return liq or None

    def _ensure_stop(self, sym: str, side: str, stop: float, qty: Decimal, tid: str) -> bool:
        for attempt in range(3):
            try:
                self.exec.place_stop(sym, side, stop, qty, tid + "s" + (str(attempt) if attempt else ""))
                orders = self.exec.protective_orders(sym)
                matches = [o for o in orders if o.kind == "STOP"]
                # Require the exchange-visible stop to cover the live quantity and correct trigger.
                for order in matches:
                    qty_ok = bool(order.close_position) or float(order.qty) + 1e-10 >= float(qty)
                    trigger_ok = abs(float(order.trigger) - float(stop)) <= max(abs(float(stop)) * 1e-4, 0.01)
                    if qty_ok and trigger_ok:
                        return True
                raise ExecutionError("stop acknowledgement not confirmed by matching protective order")
            except (ExecutionError, BinanceAPIError, NetworkError, RateLimited) as exc:
                log.error("stop placement failed (%s/3) for %s: %s", attempt + 1, sym, exc)
                try:  # a timeout may still have created the order
                    for order in self.exec.protective_orders(sym):
                        qty_ok = bool(order.close_position) or float(order.qty) + 1e-10 >= float(qty)
                        trigger_ok = abs(float(order.trigger) - float(stop)) <= max(abs(float(stop)) * 1e-4, 0.01)
                        if order.kind == "STOP" and qty_ok and trigger_ok:
                            return True
                except Exception:
                    pass
                time.sleep(0.4 * (attempt + 1))
        return False

    def _emergency_close(self, sym: str, side: str, qty: Decimal, tid: str, reason: str) -> None:
        """Close first; keep the protective stop in place until flat is confirmed."""
        routine = reason in ROUTINE_CLOSE_REASONS
        self.alert("INFO" if routine else "CRITICAL", f"{'CLOSING' if routine else 'EMERGENCY CLOSE'} {sym}: {reason}")
        self.db.event("INFO" if routine else "CRITICAL", "close_order" if routine else "emergency_close", f"{sym}: {reason}")
        for attempt in range(4):
            try:
                # Refresh actual exchange quantity on every retry; never trust stale requested qty.
                positions = self.exec.positions()
                pos = positions.get(sym)
                if pos is None:
                    self.exec.cancel_all(sym)
                    self.db.upsert_trade({"id": tid, "status": "CLOSING", "exit_reason": reason})
                    return
                remaining = Decimal(str(abs(pos.qty)))
                self.exec.close_market(sym, pos.side, remaining, f"{tid}x{attempt}")
                # A successful HTTP response is not enough: confirm exchange is flat.
                after = self.exec.positions()
                if sym not in after:
                    self.exec.cancel_all(sym)  # now safe to remove orphan protective orders
                    self.db.upsert_trade({"id": tid, "status": "CLOSING", "exit_reason": reason})
                    return
                log.warning("emergency close partially filled for %s; remaining qty=%s", sym, after[sym].qty)
            except Exception as exc:
                log.error("emergency close attempt %d failed for %s: %s", attempt + 1, sym, exc)
                # Do not cancel protective orders on any uncertain close result.
                time.sleep(0.5 * (attempt + 1))
        self.risk.halt(f"EMERGENCY CLOSE FAILED for {sym}: position may remain open; protective orders retained", "lifecycle")
        self.alert("CRITICAL", f"{sym} may still be OPEN. Protective orders were not deliberately cancelled. Trading halted; verify Binance manually.")
        self.db.event("CRITICAL", "emergency_close_failed", f"{sym}: close not confirmed; protective orders retained")

    # ------------------------------------------------------------------
    def manage(self) -> List[Dict[str, Any]]:
        """Guardian pass; returns list of trades finalised in this pass."""
        finished: List[Dict[str, Any]] = []
        try:
            positions = self.exec.positions()
            self._pos_cache = (time.time(), positions)
        except RateLimited:
            return finished
        except Exception as exc:
            log.warning("positions query failed: %s", exc)
            return finished
        # Crash/timeout in the middle of an entry (PENDING) or an entry whose outcome could not be verified
        # (ENTRY_UNKNOWN): now that positions are readable, adopt the position or discard the record.
        for t in self.db.query("SELECT * FROM trades WHERE status IN ('PENDING','ENTRY_UNKNOWN') AND ts_open < ?", (now_ms() - PENDING_GRACE_MS,)):
            pos = positions.get(t["symbol"])
            if pos is not None and pos.side == t["side"]:
                risk_actual = abs(float(pos.entry) - float(t["stop"])) * abs(float(pos.qty))
                self.db.upsert_trade({"id": t["id"], "status": "OPEN", "entry": pos.entry, "qty": abs(pos.qty), "exit_reason": None,
                                      "risk_usdt": max(risk_actual, 1e-9)})
                self.db.event("WARNING", "adopted_pending", f"{t['symbol']}: unresolved entry ({t['status']}) found on exchange, adopted; guardian will verify its stop")
            else:
                self.db.upsert_trade({"id": t["id"], "status": "FAILED", "ts_close": now_ms(),
                                      "exit_reason": "PENDING_TIMEOUT" if t["status"] == "PENDING" else "ENTRY_NOT_FOUND"})
                self.db.event("WARNING", "entry_discarded", f"{t['symbol']}: unresolved entry ({t['status']}) has no matching exchange position; discarded")
        for t in self.db.query("SELECT * FROM trades WHERE status IN ('OPEN','CLOSING')"):
            t["meta"] = self.db.get_trade(t["id"]).get("meta") or {}
            sym = t["symbol"]
            pos = positions.get(sym)
            try:
                if pos is None or abs(pos.qty) < 1e-12:
                    done = self._finalize(t)
                    if done:
                        finished.append(done)
                elif t["status"] == "CLOSING":
                    self._emergency_close(sym, pos.side, Decimal(str(abs(pos.qty))), t["id"], t.get("exit_reason") or "RETRY_CLOSE")
                else:
                    self._guard_open(t, pos)
            except RateLimited:
                continue
            except Exception as exc:
                log.exception("manage %s failed", sym)
                self.db.event("ERROR", "manage_error", f"{sym}: {type(exc).__name__}: {exc}")
        return finished

    def _guard_open(self, t: Dict[str, Any], pos) -> None:
        sym, side = t["symbol"], t["side"]
        orders = self.exec.protective_orders(sym)
        stops = [o for o in orders if o.kind == "STOP"]
        tps = [o for o in orders if o.kind == "TP"]
        qty_now = Decimal(str(abs(pos.qty)))
        liq = float(getattr(pos, "liq_price", 0.0) or 0.0)
        if liq and stop_beyond_liquidation(side, float(t["stop"]), liq, LIQ_MARGIN):
            self.db.event("CRITICAL", "stop_beyond_liquidation", f"{sym}: exchange liquidation {liq:.6g} reached the stop {float(t['stop']):.6g}; closing")
            self._emergency_close(sym, side, qty_now, t["id"], "STOP_BEYOND_LIQUIDATION")
            return
        # A visible stop is not sufficient: it must protect the current position
        # quantity and match the latest persisted stop trigger (BE/trailing included).
        level = float(t["stop"])
        trigger_tol = max(abs(level) * 1e-4, 0.01)
        valid_stops = [o for o in stops
                       if (o.close_position or float(o.qty) + 1e-10 >= float(qty_now))
                       and abs(float(o.trigger) - level) <= trigger_tol]
        if not valid_stops:
            code = "stop_missing" if not stops else "stop_coverage_mismatch"
            self.db.event("CRITICAL", code,
                          f"{sym}: no exchange-visible stop covers qty={qty_now} at stored trigger={level:.8g}; repairing")
            if not self._ensure_stop(sym, side, level, qty_now, t["id"] + "g"):
                self._emergency_close(sym, side, qty_now, t["id"], "STOP_LOST")
                return
            # Keep the old stop until a replacement has been confirmed; only then
            # remove stale/undersized/wrong-trigger stops.
            orders = self.exec.protective_orders(sym)
            valid_stops = [o for o in orders if o.kind == "STOP"
                           and (o.close_position or float(o.qty) + 1e-10 >= float(qty_now))
                           and abs(float(o.trigger) - level) <= trigger_tol]
            if not valid_stops:
                self._emergency_close(sym, side, qty_now, t["id"], "STOP_LOST")
                return
            for stale in orders:
                if stale.kind == "STOP" and stale not in valid_stops:
                    self.exec.cancel_protective(sym, stale.order_id)
        else:
            # Remove duplicate stale stops only after a fully covering stop is known.
            for stale in stops:
                if stale not in valid_stops:
                    self.exec.cancel_protective(sym, stale.order_id)
        # TP1 filled? (position shrank) -> move stop to break-even once
        meta = dict(t.get("meta") or {})
        just_moved = False
        if not meta.get("be_moved") and float(qty_now) < float(t["qty"]) * 0.75:
            just_moved = True
            be = t["entry"] * (1 + BE_FEE_BUFFER) if side == "LONG" else t["entry"] * (1 - BE_FEE_BUFFER)
            try:
                new_id = self.exec.place_stop(sym, side, be, qty_now, t["id"] + "b")
                for o in stops:
                    if o.order_id != new_id:
                        self.exec.cancel_protective(sym, o.order_id)
                meta["be_moved"] = True
                self.db.upsert_trade({"id": t["id"], "meta": meta, "stop": be})
                self.db.event("INFO", "break_even", f"{sym}: TP1 filled, stop moved to break-even {be:.6g}")
                self.alert("INFO", f"{sym}: TP1 hit, stop moved to break-even")
            except Exception as exc:
                self.db.event("ERROR", "be_failed", f"{sym}: break-even move failed (original stop kept): {exc}")
        elif not just_moved:
            self._trail_stop(t, pos, meta, stops, qty_now)
        # time stop
        style = STYLES.get(t["style"])
        if style and now_ms() - t["ts_open"] > style.max_hold_minutes * 60_000:
            self.db.event("INFO", "time_stop", f"{sym}: max hold {style.max_hold_minutes} min exceeded")
            self._emergency_close(sym, side, qty_now, t["id"], "TIME_STOP")
            return
        # missing take-profit after a failed placement: re-create the final target
        if not tps and not meta.get("be_moved") and not meta.get("tp_retry"):
            try:
                self.exec.place_tp(sym, side, t["tp2"], qty_now, t["id"] + "r")
                meta["tp_retry"] = True
                self.db.upsert_trade({"id": t["id"], "meta": meta})
            except Exception as exc:
                self.db.event("ERROR", "tp_retry_failed", f"{sym}: {exc}")

    def _trail_stop(self, t: Dict[str, Any], pos, meta: Dict[str, Any], stops: list, qty_now: Decimal) -> None:
        """After TP1 the runner's stop follows the mark price at TRAIL_ATR_MULT x ATR. It only ever moves in favour."""
        mult, a, mark = self.cfg.trail_atr_mult, float(meta.get("atr") or 0), float(pos.mark or 0)
        if not meta.get("be_moved") or mult <= 0 or a <= 0 or mark <= 0 or not stops:
            return
        sym, side, cur = t["symbol"], t["side"], float(t["stop"])
        want = mark - mult * a if side == "LONG" else mark + mult * a
        improved = want > cur + TRAIL_MIN_STEP_ATR * a if side == "LONG" else want < cur - TRAIL_MIN_STEP_ATR * a
        if not improved:
            return
        n = int(meta.get("trail_n") or 0) + 1
        try:
            new_id = self.exec.place_stop(sym, side, want, qty_now, f"{t['id']}t{n}")     # new stop FIRST, old one after
            for o in stops:
                if o.order_id != new_id:
                    self.exec.cancel_protective(sym, o.order_id)
            meta["trail_n"] = n
            self.db.upsert_trade({"id": t["id"], "meta": meta, "stop": want})
            self.db.event("INFO", "trail_stop", f"{sym}: stop trailed to {want:.6g} ({mult:g} ATR behind mark {mark:.6g})")
        except Exception as exc:
            self.db.event("ERROR", "trail_failed", f"{sym}: trailing stop move failed (previous stop kept): {exc}")

    def _finalize(self, t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sym, tid = t["symbol"], t["id"]
        # Second, fresh confirmation that the position is really flat before touching any protective order.
        if sym in self.exec.positions():
            return None
        try:
            self.exec.cancel_all(sym)   # orphan protective orders (reduce-only / close-position: harmless but untidy)
        except Exception as exc:
            log.warning("orphan cancel failed for %s: %s", sym, exc)
        summ = self.exec.exit_summary(sym, t["ts_open"] - FILL_TIME_SLACK_MS, t["side"])
        n = self._finalize_attempts.get(tid, 0) + 1
        self._finalize_attempts[tid] = n
        if not summ.complete:
            if n < FINALIZE_RETRIES:
                return None   # fills not visible yet; try again next pass
            # Gave up: the position is flat but the exit fills never showed up. Close the record WITHOUT inventing a PnL and
            # without feeding the loss-streak counter; the equity-based daily-loss / drawdown limits still see the real outcome.
            self.db.upsert_trade({"id": tid, "status": "CLOSED", "ts_close": now_ms(), "exit_reason": "EXIT_UNVERIFIED"})
            self._finalize_attempts.pop(tid, None)
            self.db.event("WARNING", "exit_unverified", f"{sym}: position is flat but no exit fills were found; PnL unknown", {"id": tid})
            self.alert("WARNING", f"{sym}: trade closed but exit fills could not be verified (PnL unknown). Check Binance trade history.")
            return self.db.get_trade(tid)
        entry = float(t["entry"])
        exit_px = summ.avg_exit or entry
        pnl = summ.realized - summ.fees + summ.funding
        risk = max(float(t["risk_usdt"] or 0), 1e-9)   # original planned risk (kept after break-even moves)
        r_mult = pnl / risk
        reason = t.get("exit_reason") or self._classify_exit(t, exit_px)
        meta = dict(t.get("meta") or {})
        meta["funding_usdt"] = round(summ.funding, 6)
        self.db.upsert_trade({"id": tid, "status": "CLOSED", "ts_close": now_ms(), "exit_price": exit_px, "pnl_usdt": round(pnl, 6),
                              "fees_usdt": round(summ.fees, 6), "r_multiple": round(r_mult, 4), "exit_reason": reason, "meta": meta})
        self.risk.record_result(r_mult)
        self._finalize_attempts.pop(tid, None)
        self.db.event("INFO", "trade_closed", f"{t['side']} {sym} closed @ {exit_px:.6g} pnl {pnl:+.3f} USDT ({r_mult:+.2f}R) {reason}", {"id": tid})
        self.alert("INFO", f"CLOSED {t['side']} {sym} {pnl:+.2f} USDT ({r_mult:+.2f}R) {reason}")
        return self.db.get_trade(tid)

    @staticmethod
    def _classify_exit(t: Dict[str, Any], exit_px: float) -> str:
        long_ = t["side"] == "LONG"
        m = t.get("meta") or {}
        if m.get("trail_n") and not ((long_ and exit_px >= t["tp2"] * 0.999) or ((not long_) and exit_px <= t["tp2"] * 1.001)):
            return "TRAILING_STOP"
        if m.get("be_moved") and abs(exit_px - t["entry"]) / t["entry"] < 0.003:
            return "BREAK_EVEN_STOP"
        if (long_ and exit_px >= t["tp2"] * 0.999) or ((not long_) and exit_px <= t["tp2"] * 1.001):
            return "TARGET"
        if (long_ and exit_px >= t["tp1"] * 0.999) or ((not long_) and exit_px <= t["tp1"] * 1.001):
            return "TARGET_1"
        if (long_ and exit_px <= t["stop"] * 1.002) or ((not long_) and exit_px >= t["stop"] * 0.998):
            return "STOP"
        return "CLOSED"

    # ------------------------------------------------------------------
    def flatten_all(self, reason: str = "manual flatten") -> List[str]:
        """Admin/kill action: cancel orders and close every position THIS system opened."""
        closed = []
        positions = self.exec.positions()
        for t in self.open_trades():
            pos = positions.get(t["symbol"])
            if pos:
                self._emergency_close(t["symbol"], pos.side, Decimal(str(abs(pos.qty))), t["id"], reason.upper().replace(" ", "_"))
                closed.append(t["symbol"])
        return closed
