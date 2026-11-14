"""Risk manager: persistent limits, kill switch, loss pauses and exchange-filter-aware sizing.

Everything here is fail-closed and persisted in SQLite so a restart cannot reset the limits.
"""
from __future__ import annotations

import logging
import math
import threading
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..config import Settings, StyleConfig
from ..db import Database
from ..exchange.filters import SymbolFilters
from ..models import TradeSetup
from .liquidation import assess
from ..util import now_ms, utc_day

log = logging.getLogger("imperion.risk")

MAJORS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"})
CORRELATED = {s: set(MAJORS - {s}) for s in MAJORS}      # two majors in the same direction are one bet
MAX_SAME_DIRECTION = 2
SIZING_AVAILABLE_FRACTION = 0.90
LIQUIDATION_SAFETY = 0.70       # the stop must sit within this share of the distance to liquidation


class RiskManager:
    def __init__(self, cfg: Settings, db: Database):
        self.cfg, self.db = cfg, db
        self._lock = threading.RLock()

    # ---- halt / kill switch -------------------------------------------
    def halted(self) -> Optional[str]:
        if self.cfg.kill_switch:
            return "KILL_SWITCH env var is set"
        h = self.db.kv_get("risk.halt")
        return h.get("reason") if isinstance(h, dict) and h.get("on") else None

    def halt(self, reason: str, by: str = "system") -> None:
        self.db.kv_set("risk.halt", {"on": True, "reason": reason, "by": by, "ts": now_ms()})
        self.db.event("CRITICAL", "halt", f"Trading halted by {by}: {reason}")
        log.critical("TRADING HALTED (%s): %s", by, reason)

    def resume(self, equity: Optional[float] = None, by: str = "admin", rebaseline: bool = True) -> None:
        """Human override: clears the halt and re-baselines the drawdown / daily-loss reference.

        The references live in "allocated equity" units (see update_equity), never raw wallet equity, so they are
        cleared here and re-seeded by the next update_equity() call. Writing the raw wallet balance would trip the
        drawdown limit again immediately whenever the wallet is larger than TRADING_CAPITAL_USDT.
        `equity` is accepted for backwards compatibility and is not stored.
        """
        self.db.kv_set("risk.halt", {"on": False, "reason": "", "by": by, "ts": now_ms()})
        self.db.kv_set("risk.consec_losses", 0)
        self.db.kv_set("risk.pause_until", 0)
        if rebaseline:
            self.db.kv_set("risk.peak_equity", 0.0)
            self.db.kv_set("risk.day_start_equity", None)
        self.db.event("WARNING", "resume", f"Trading resumed by {by}")

    # ---- equity bookkeeping -------------------------------------------
    def trading_equity(self, actual_equity: float) -> float:
        """Capital the system is allowed to size against (never more than TRADING_CAPITAL_USDT)."""
        return max(0.0, min(actual_equity, self.cfg.trading_capital_usdt))

    def update_equity(self, equity: float) -> Optional[str]:
        """Roll the UTC day, track peak, and auto-halt on daily-loss / drawdown. Returns halt reason if tripped."""
        if not math.isfinite(equity) or equity < 0:
            return None
        # All limits use the strategy allocation, not the entire exchange wallet.
        # This prevents a 1% wallet fluctuation from being measured against a smaller
        # allocated-capital denominator (or a large wallet from loosening limits).
        with self._lock:
            baseline_actual = self.db.kv_get("risk.baseline_actual_equity")
            baseline_allocated = self.db.kv_get("risk.baseline_allocated_equity")
            if baseline_actual is None:
                baseline_actual = float(equity)
                baseline_allocated = self.trading_equity(equity)
                self.db.kv_set("risk.baseline_actual_equity", baseline_actual)
                self.db.kv_set("risk.baseline_allocated_equity", baseline_allocated)
            # Attribute wallet PnL to the configured allocation while keeping a stable
            # baseline across restarts. External/manual account activity should be avoided.
            equity_ref = max(0.0, float(baseline_allocated) + (float(equity) - float(baseline_actual)))
            if equity_ref <= 0:
                self.halt("allocated strategy equity depleted", "risk-manager")
                return "allocated strategy equity depleted"
            day = utc_day()
            if self.db.kv_get("risk.day") != day:
                self.db.kv_set("risk.day", day)
                self.db.kv_set("risk.day_start_equity", equity_ref)
                self.db.kv_set("risk.daily_new_risk", 0.0)
                h = self.db.kv_get("risk.halt")
                # The daily-loss halt is a *daily* limit: it lapses with the UTC day. Drawdown / system halts never auto-clear.
                if isinstance(h, dict) and h.get("on") and h.get("by") == "risk-manager" and str(h.get("reason", "")).startswith("daily loss limit"):
                    self.resume(by="new-day", rebaseline=False)   # drawdown reference (peak) must survive the day roll
                    self.db.kv_set("risk.day_start_equity", equity_ref)
            start = self.db.kv_get("risk.day_start_equity")
            if not start:
                self.db.kv_set("risk.day_start_equity", equity_ref); start = equity_ref
            peak = max(self.db.kv_get("risk.peak_equity", 0.0) or 0.0, equity_ref)
            self.db.kv_set("risk.peak_equity", peak)
            reason = None
            # Percentages are measured against the allocated-equity proxy, not raw wallet equity.
            day_base, peak_base = start, peak
            # Drawdown is checked first: if both limits are breached the (non-lapsing) drawdown halt takes precedence.
            if self.cfg.max_drawdown_pct > 0 and peak_base > 0 and (peak - equity_ref) / peak_base * 100 >= self.cfg.max_drawdown_pct:
                reason = f"max drawdown {self.cfg.max_drawdown_pct}% reached (allocated equity {equity_ref:.2f} vs peak {peak:.2f})"
            elif self.cfg.daily_loss_limit_pct > 0 and day_base > 0 and (equity_ref - start) / day_base * 100 <= -self.cfg.daily_loss_limit_pct:
                reason = f"daily loss limit {self.cfg.daily_loss_limit_pct}% reached (allocated equity {equity_ref:.2f} vs day start {start:.2f})"
            if reason and not self.halted():
                self.halt(reason, "risk-manager")
            return reason

    def status(self, equity: Optional[float] = None) -> Dict[str, Any]:
        eq_ref = self.db.kv_get("risk.day_start_equity")
        return {
            "halted": self.halted(), "day": self.db.kv_get("risk.day"), "day_start_equity": eq_ref,
            "peak_equity": self.db.kv_get("risk.peak_equity"), "daily_new_risk_pct": round((self.db.kv_get("risk.daily_new_risk", 0.0) or 0.0) * 100, 3),
            "consecutive_losses": self.db.kv_get("risk.consec_losses", 0),
            "paused_until": self.db.kv_get("risk.pause_until", 0), "equity": equity,
        }

    # ---- results ------------------------------------------------------
    def record_result(self, r_multiple: float) -> None:
        with self._lock:
            if r_multiple < -0.05:
                n = (self.db.kv_get("risk.consec_losses", 0) or 0) + 1
                self.db.kv_set("risk.consec_losses", n)
                if self.cfg.consecutive_loss_pause > 0 and n >= self.cfg.consecutive_loss_pause:
                    until = now_ms() + self.cfg.loss_pause_minutes * 60_000
                    self.db.kv_set("risk.pause_until", until)
                    self.db.event("WARNING", "loss_pause", f"{n} consecutive losses: new entries paused {self.cfg.loss_pause_minutes} min")
            elif r_multiple > 0.05:
                self.db.kv_set("risk.consec_losses", 0)

    def register_new_risk(self, risk_usdt: float, trading_equity: float) -> None:
        with self._lock:
            used = (self.db.kv_get("risk.daily_new_risk", 0.0) or 0.0) + risk_usdt / max(trading_equity, 1e-9)
            self.db.kv_set("risk.daily_new_risk", used)

    # ---- entry gate ---------------------------------------------------
    def open_risk_usdt(self, open_trades: List[Dict[str, Any]]) -> float:
        return sum(float(t.get("risk_usdt") or 0.0) for t in open_trades if not (t.get("meta") or {}).get("be_moved"))

    def precheck(self, symbol: str, style: StyleConfig, side: Optional[str], open_trades: List[Dict[str, Any]],
                 trading_equity: float, new_risk_usdt: Optional[float] = None) -> Dict[str, Any]:
        """Side-agnostic (side=None) or side-specific limit checks. Returns {allowed, reasons, checks}."""
        reasons: List[str] = []
        halt = self.halted()
        if halt:
            reasons.append(f"HALTED:{halt}")
        pause = self.db.kv_get("risk.pause_until", 0) or 0
        if pause > now_ms():
            reasons.append(f"LOSS_PAUSE_ACTIVE ({(pause - now_ms()) / 60000:.0f} min left)")
        if trading_equity < self.cfg.min_equity_usdt:
            reasons.append(f"EQUITY_BELOW_MIN ({trading_equity:.2f}<{self.cfg.min_equity_usdt})")
        position_ok = len(open_trades) < self.cfg.max_concurrent_positions
        if any((t.get("meta") or {}).get("unknown") for t in open_trades):
            reasons.append("EXPOSURE_UNKNOWN (exchange positions could not be read)")
            position_ok = False
        if not position_ok:
            reasons.append(f"MAX_POSITIONS ({len(open_trades)}/{self.cfg.max_concurrent_positions})")
        if any(t["symbol"] == symbol for t in open_trades):
            reasons.append("SYMBOL_ALREADY_HAS_POSITION")
            position_ok = False
        last = self.db.one("SELECT MAX(ts_open) AS ts FROM trades WHERE symbol=? AND style=?", (symbol, style.name))
        if last and last["ts"] and now_ms() - last["ts"] < style.cooldown_minutes * 60_000:
            reasons.append(f"COOLDOWN_{style.name.upper()} ({style.cooldown_minutes} min)")
            position_ok = False

        per_trade = trading_equity * self.cfg.risk_per_trade_pct / 100
        risk = new_risk_usdt if new_risk_usdt is not None else per_trade
        if not math.isfinite(risk) or risk <= 0 or risk > per_trade + 1e-9:
            reasons.append("PER_TRADE_RISK_LIMIT_OR_INVALID")
        open_risk = self.open_risk_usdt(open_trades)
        budget_ok = True
        if (open_risk + risk) / max(trading_equity, 1e-9) * 100 > self.cfg.max_open_risk_pct + 1e-9:
            budget_ok = False
            reasons.append(f"OPEN_RISK_LIMIT ({(open_risk + risk) / trading_equity * 100:.2f}% > {self.cfg.max_open_risk_pct}%)")
        daily = (self.db.kv_get("risk.daily_new_risk", 0.0) or 0.0) + risk / max(trading_equity, 1e-9)
        if daily * 100 > self.cfg.max_daily_new_risk_pct + 1e-9:
            budget_ok = False
            reasons.append(f"DAILY_NEW_RISK_LIMIT ({daily * 100:.2f}% > {self.cfg.max_daily_new_risk_pct}%)")
        # Day Trade requested policy: no arbitrary 2-same-direction or
        # major-correlation veto. Exposure, stop and margin checks remain active.
        halted_or_paused = bool(halt) or pause > now_ms() or trading_equity < self.cfg.min_equity_usdt
        return {
            "allowed": not reasons,
            "reasons": reasons,
            "risk_budget_ok": budget_ok and not halted_or_paused,
            "position_limit_ok": position_ok and not halted_or_paused,
        }

    # ---- sizing -------------------------------------------------------
    def size(self, setup: TradeSetup, filt: SymbolFilters, trading_equity: float, available_balance: float,
             brackets: Optional[List[Dict[str, float]]] = None, risk_multiplier: float = 1.0) -> Dict[str, Any]:
        """Exchange-filter-aware size. `brackets` are the symbol's leverage brackets from the exchange; without them a
        conservative default maintenance margin is used (dry mode)."""
        cfg = self.cfg
        # Inventory / toxicity may reduce planned risk, never increase it.
        risk_multiplier = max(0.0, min(1.0, float(risk_multiplier)))
        if risk_multiplier <= 0:
            return {"ok": False, "reason": "RISK_MULTIPLIER_ZERO"}
        risk_usdt = trading_equity * cfg.risk_per_trade_pct / 100 * risk_multiplier
        entry = setup.entry
        fee_per_unit = entry * (2 * cfg.taker_fee_pct / 100)
        # Reserve both legs of expected slippage as well as round-trip taker fees.
        # The fill-time hard cap is still checked in Lifecycle and may emergency-close.
        slip_per_unit = entry * (2 * cfg.slippage_pct / 100)
        unit_risk = abs(entry - setup.stop) + fee_per_unit + slip_per_unit
        if not all(math.isfinite(v) for v in (entry, setup.stop, unit_risk)) or unit_risk <= 0 or entry <= 0:
            return {"ok": False, "reason": "SIZE_INVALID_INPUT"}
        qty = filt.floor_qty(risk_usdt / unit_risk)
        max_notional = trading_equity * cfg.leverage * 0.95
        if float(qty) * entry > max_notional:
            qty = filt.floor_qty(max_notional / entry)
        floor_notional = filt.min_notional * Decimal("1.02")
        if not filt.qty_ok(qty) or Decimal(str(float(qty) * entry)) < floor_notional:
            qmin = filt.min_qty_for_notional(entry)
            if float(qmin) * unit_risk <= risk_usdt * cfg.max_risk_overshoot and float(qmin) * entry <= max_notional and filt.qty_ok(qmin):
                qty = qmin
            else:
                return {"ok": False, "reason": f"SIZE_BELOW_EXCHANGE_MIN (needs {qmin} {setup.symbol} ≈ risk {float(qmin) * unit_risk:.2f} USDT vs allowed {risk_usdt * cfg.max_risk_overshoot:.2f})"}
        notional = float(qty) * entry
        margin = notional / cfg.leverage
        if margin > available_balance * SIZING_AVAILABLE_FRACTION:
            return {"ok": False, "reason": f"INSUFFICIENT_MARGIN (need {margin:.2f}, available {available_balance:.2f})"}
        # Isolated margin: a stop at or beyond liquidation never executes as a stop, so the planned risk would be fiction.
        liq = assess(setup.side, entry, setup.stop, float(qty), cfg.leverage, brackets, LIQUIDATION_SAFETY)
        if not liq["ok"]:
            return {"ok": False, "reason": "; ".join(liq["reasons"]), "liquidation": liq}
        actual_risk = float(qty) * unit_risk
        # TP split: half at TP1 when both legs are valid exchange quantities
        q1 = filt.floor_qty(float(qty) / 2)
        q2 = qty - q1
        split = filt.qty_ok(q1) and filt.qty_ok(q2)
        return {"ok": True, "qty": qty, "notional": notional, "margin": margin, "risk_usdt": actual_risk,
                "planned_risk_usdt": risk_usdt, "risk_multiplier": risk_multiplier,
                "tp1_qty": q1 if split else Decimal(0), "tp2_qty": q2 if split else qty,
                "split": split, "liq_price": liq["liq_price"], "liquidation": liq}
