"""Executor contract shared by the Binance adapter and the dry-run simulator."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Protocol

from ..exchange.filters import SymbolFilters


@dataclass
class Account:
    equity: float          # wallet + unrealized
    available: float
    unrealized: float = 0.0


@dataclass
class Position:
    symbol: str
    qty: float             # signed: >0 long, <0 short
    entry: float
    mark: float = 0.0
    upnl: float = 0.0
    liq_price: float = 0.0   # exchange liquidation price when available

    @property
    def side(self) -> str:
        return "LONG" if self.qty > 0 else "SHORT"


@dataclass
class Fill:
    qty: Decimal
    avg_price: float
    order_id: str = ""
    status: str = ""


@dataclass
class ProtectiveOrder:
    order_id: str
    kind: str              # STOP | TP
    trigger: float
    qty: float             # 0 when closePosition
    close_position: bool = False
    client_id: str = ""


@dataclass
class ExitSummary:
    qty: float
    avg_exit: float
    realized: float
    fees: float
    complete: bool = True
    funding: float = 0.0       # signed funding income over the holding period (negative = paid)


class ExecutionError(Exception):
    pass


class Executor(Protocol):
    mode: str

    def filters(self, symbol: str) -> SymbolFilters: ...
    def leverage_brackets(self, symbol: str) -> Optional[List[Dict[str, float]]]: ...
    def account(self) -> Account: ...
    def positions(self) -> Dict[str, Position]: ...
    def prepare(self, symbol: str) -> None: ...
    def enter(self, symbol: str, side: str, qty: Decimal, client_id: str) -> Fill: ...
    def place_stop(self, symbol: str, side: str, trigger: float, qty: Optional[Decimal], client_id: str) -> str: ...
    def place_tp(self, symbol: str, side: str, trigger: float, qty: Decimal, client_id: str) -> str: ...
    def protective_orders(self, symbol: str) -> List[ProtectiveOrder]: ...
    def cancel_protective(self, symbol: str, order_id: str) -> None: ...
    def cancel_all(self, symbol: str) -> None: ...
    def close_market(self, symbol: str, side: str, qty: Decimal, client_id: str) -> Fill: ...
    def exit_summary(self, symbol: str, since_ms: int, entry_side: str) -> ExitSummary: ...
