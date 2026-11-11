"""Exchange filters (tick/step/min notional) and exact decimal quantisation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Dict


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


def fmt(x: Any) -> str:
    """Plain decimal string (no exponent) suitable for Binance parameters."""
    d = x if isinstance(x, Decimal) else _d(x)
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    status: str = "TRADING"
    contract_type: str = "PERPETUAL"
    tick_size: Decimal = Decimal("0.01")
    step_size: Decimal = Decimal("0.001")
    min_qty: Decimal = Decimal("0.001")
    max_qty: Decimal = Decimal("1000000")
    market_step: Decimal = Decimal("0.001")
    market_min_qty: Decimal = Decimal("0.001")
    market_max_qty: Decimal = Decimal("1000000")
    min_notional: Decimal = Decimal("5")

    def floor_qty(self, qty: float, market: bool = True) -> Decimal:
        step = self.market_step if market else self.step_size
        return (_d(qty) / step).to_integral_value(ROUND_FLOOR) * step

    def round_price(self, price: float, mode: str = "nearest") -> Decimal:
        rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING}.get(mode, ROUND_HALF_UP)
        return (_d(price) / self.tick_size).to_integral_value(rounding) * self.tick_size

    def qty_ok(self, qty: Decimal, market: bool = True) -> bool:
        lo = self.market_min_qty if market else self.min_qty
        hi = self.market_max_qty if market else self.max_qty
        return lo <= qty <= hi

    def min_qty_for_notional(self, price: float, margin: float = 1.02) -> Decimal:
        """Smallest valid market quantity whose notional clears min_notional (+margin)."""
        if price <= 0:
            return self.market_min_qty
        need = (self.min_notional * _d(margin)) / _d(price)
        steps = (need / self.market_step).to_integral_value(ROUND_CEILING)
        return max(steps * self.market_step, self.market_min_qty)


def parse_exchange_info(data: Dict[str, Any]) -> Dict[str, SymbolFilters]:
    out: Dict[str, SymbolFilters] = {}
    for s in data.get("symbols", []):
        f = {x.get("filterType"): x for x in s.get("filters", [])}
        price_f, lot, mlot, notional = f.get("PRICE_FILTER", {}), f.get("LOT_SIZE", {}), f.get("MARKET_LOT_SIZE", {}), f.get("MIN_NOTIONAL", {})
        try:
            out[s["symbol"]] = SymbolFilters(
                symbol=s["symbol"],
                status=s.get("status", ""),
                contract_type=s.get("contractType", ""),
                tick_size=_d(price_f.get("tickSize", "0.01")),
                step_size=_d(lot.get("stepSize", "0.001")),
                min_qty=_d(lot.get("minQty", "0.001")),
                max_qty=_d(lot.get("maxQty", "1000000")),
                market_step=_d(mlot.get("stepSize", lot.get("stepSize", "0.001"))),
                market_min_qty=_d(mlot.get("minQty", lot.get("minQty", "0.001"))),
                market_max_qty=_d(mlot.get("maxQty", lot.get("maxQty", "1000000"))),
                min_notional=_d(notional.get("notional", "5")),
            )
        except Exception:
            continue
    return out
