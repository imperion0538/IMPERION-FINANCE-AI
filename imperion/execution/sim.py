"""Dry-run executor: real market data, simulated fills (used by MODE=dry and the test-suite)."""
from __future__ import annotations

import itertools
import threading
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from ..config import Settings
from ..exchange.filters import SymbolFilters
from ..risk.liquidation import DEFAULT_BRACKETS, bracket_for, isolated_liquidation_price
from ..util import now_ms
from .base import Account, ExecutionError, ExitSummary, Fill, Position, ProtectiveOrder


class SimExecutor:
    mode = "dry"

    def __init__(self, cfg: Settings, filters: Dict[str, SymbolFilters], price_fn: Callable[[str], Optional[float]],
                 start_balance: Optional[float] = None):
        self.cfg, self._filters, self._price = cfg, filters, price_fn
        self.balance = float(start_balance if start_balance is not None else cfg.trading_capital_usdt)
        self.pos: Dict[str, Dict[str, float]] = {}
        self.orders: List[Dict] = []
        self.fills: List[Dict] = []
        self._ids = itertools.count(1)
        self._last_seen: Dict[str, float] = {}
        self._lock = threading.RLock()
        self.fail_next_stop = False      # test hook

    def filters(self, symbol: str) -> SymbolFilters:
        return self._filters[symbol]

    # ---- account -------------------------------------------------------
    def account(self) -> Account:
        with self._lock:
            up = sum(self._upnl(s) for s in self.pos)
            margin = sum(abs(p["qty"]) * p["entry"] / self.cfg.leverage for p in self.pos.values())
            return Account(equity=self.balance + up, available=max(0.0, self.balance + up - margin), unrealized=up)

    def _upnl(self, symbol: str) -> float:
        p = self.pos[symbol]
        px = self._price(symbol) or p["entry"]
        return (px - p["entry"]) * p["qty"]

    def positions(self) -> Dict[str, Position]:
        with self._lock:
            out = {}
            for s, p in self.pos.items():
                if p["qty"] == 0:
                    continue
                side = "LONG" if p["qty"] > 0 else "SHORT"
                br = bracket_for(DEFAULT_BRACKETS, abs(p["qty"]) * p["entry"])
                liq = isolated_liquidation_price(side, p["entry"], abs(p["qty"]), self.cfg.leverage, br["maintMarginRatio"], br["cum"])
                out[s] = Position(s, p["qty"], p["entry"], self._price(s) or p["entry"], self._upnl(s), liq)
            return out

    def leverage_brackets(self, symbol: str) -> None:
        return None                       # simulator: sizing uses the conservative default brackets

    def prepare(self, symbol: str) -> None:
        return None

    # ---- orders --------------------------------------------------------
    def _fee(self, notional: float) -> float:
        return notional * self.cfg.taker_fee_pct / 100

    def _fill(self, symbol: str, side: str, qty: float, price: float, kind: str) -> None:
        self.balance -= self._fee(qty * price)
        self.fills.append({"symbol": symbol, "side": side, "qty": qty, "price": price, "ts": now_ms(), "kind": kind,
                           "fee": self._fee(qty * price), "realized": 0.0})

    def enter(self, symbol: str, side: str, qty: Decimal, client_id: str) -> Fill:
        with self._lock:
            if symbol in self.pos and self.pos[symbol]["qty"] != 0:
                raise ExecutionError("position already open (one-way mode)")
            px = self._price(symbol)
            if not px:
                raise ExecutionError("no price")
            slip = self.cfg.slippage_pct / 100
            fill_px = px * (1 + slip) if side == "LONG" else px * (1 - slip)
            q = float(qty)
            self.pos[symbol] = {"qty": q if side == "LONG" else -q, "entry": fill_px, "ts": now_ms()}
            self._fill(symbol, "BUY" if side == "LONG" else "SELL", q, fill_px, "entry")
            return Fill(qty, fill_px, str(next(self._ids)), "FILLED")

    def _add_order(self, symbol, kind, trigger, qty, close_position, client_id) -> str:
        oid = str(next(self._ids))
        self.orders.append({"id": oid, "symbol": symbol, "kind": kind, "trigger": float(trigger), "qty": float(qty or 0),
                            "close_position": close_position, "client_id": client_id})
        return oid

    def place_stop(self, symbol: str, side: str, trigger: float, qty: Optional[Decimal], client_id: str) -> str:
        if self.fail_next_stop:
            self.fail_next_stop = False
            raise ExecutionError("simulated stop rejection")
        return self._add_order(symbol, "STOP", trigger, qty, True, client_id)

    def place_tp(self, symbol: str, side: str, trigger: float, qty: Decimal, client_id: str) -> str:
        return self._add_order(symbol, "TP", trigger, qty, False, client_id)

    def protective_orders(self, symbol: str) -> List[ProtectiveOrder]:
        with self._lock:
            return [ProtectiveOrder(o["id"], o["kind"], o["trigger"], o["qty"], o["close_position"], o["client_id"])
                    for o in self.orders if o["symbol"] == symbol]

    def cancel_protective(self, symbol: str, order_id: str) -> None:
        with self._lock:
            self.orders = [o for o in self.orders if o["id"] != order_id]

    def cancel_all(self, symbol: str) -> None:
        with self._lock:
            self.orders = [o for o in self.orders if o["symbol"] != symbol]

    def _reduce(self, symbol: str, qty: float, price: float, kind: str) -> None:
        p = self.pos[symbol]
        sign = 1 if p["qty"] > 0 else -1
        qty = min(qty, abs(p["qty"]))
        pnl = (price - p["entry"]) * qty * sign
        self.balance += pnl
        self._fill(symbol, "SELL" if sign > 0 else "BUY", qty, price, kind)
        self.fills[-1]["realized"] = pnl
        p["qty"] -= sign * qty
        if abs(p["qty"]) < 1e-12:
            del self.pos[symbol]
            self.orders = [o for o in self.orders if o["symbol"] != symbol]

    def close_market(self, symbol: str, side: str, qty: Decimal, client_id: str) -> Fill:
        with self._lock:
            if symbol not in self.pos:
                return Fill(Decimal(0), 0.0, "", "NOOP")
            px = self._price(symbol) or self.pos[symbol]["entry"]
            slip = self.cfg.slippage_pct / 100
            fill_px = px * (1 - slip) if side == "LONG" else px * (1 + slip)
            self._reduce(symbol, float(qty), fill_px, "market_close")
            return Fill(qty, fill_px, str(next(self._ids)), "FILLED")

    def process_price(self, symbol: str, last: float) -> None:
        """Trigger stops/TPs crossed between the previous and current price. Stop wins ties (conservative)."""
        with self._lock:
            prev = self._last_seen.get(symbol, last)
            self._last_seen[symbol] = last
            if symbol not in self.pos:
                return
            lo, hi = min(prev, last), max(prev, last)
            p = self.pos[symbol]
            long_ = p["qty"] > 0
            orders = sorted(self.orders, key=lambda o: 0 if o["kind"] == "STOP" else 1)
            for o in orders:
                if o["symbol"] != symbol or symbol not in self.pos:
                    continue
                if o["kind"] == "STOP":
                    hit = (lo <= o["trigger"]) if long_ else (hi >= o["trigger"])
                else:
                    hit = (hi >= o["trigger"]) if long_ else (lo <= o["trigger"])
                if hit:
                    qty = abs(p["qty"]) if o["close_position"] or o["qty"] == 0 else o["qty"]
                    slip = self.cfg.slippage_pct / 100 if o["kind"] == "STOP" else 0.0
                    px = o["trigger"] * ((1 - slip) if long_ else (1 + slip))
                    self.orders = [x for x in self.orders if x["id"] != o["id"]]
                    self._reduce(symbol, qty, px, o["kind"].lower())

    def exit_summary(self, symbol: str, since_ms: int, entry_side: str) -> ExitSummary:
        exit_side = "SELL" if entry_side == "LONG" else "BUY"
        rows = [f for f in self.fills if f["symbol"] == symbol and f["ts"] >= since_ms - 1000]
        ex = [f for f in rows if f["side"] == exit_side]
        q = sum(f["qty"] for f in ex)
        return ExitSummary(qty=q, avg_exit=(sum(f["price"] * f["qty"] for f in ex) / q) if q else 0.0,
                           realized=sum(f["realized"] for f in rows), fees=sum(f["fee"] for f in rows), complete=q > 0)
