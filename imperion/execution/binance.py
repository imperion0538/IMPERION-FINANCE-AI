"""Binance USDⓈ-M Futures executor.

API facts used (docs checked Nov 2026):
  * MARKET / LIMIT orders: POST /fapi/v1/order
  * Conditional orders (STOP_MARKET, TAKE_PROFIT_MARKET, ...) MUST use the Algo Service since 2025-12-09:
      POST /fapi/v1/algoOrder  (algoType=CONDITIONAL, triggerPrice, closePosition | quantity+reduceOnly,
      clientAlgoId); list GET /fapi/v1/openAlgoOrders; cancel DELETE /fapi/v1/algoOrder (algoId) and
      DELETE /fapi/v1/algoOpenOrders (symbol). The legacy endpoint answers -4120 for these types.
  * Algo order requests cost 1 IP weight and count against the 10s / 1min ORDER rate limits.
  * openAlgoOrders returns a bare JSON array; closePosition / reduceOnly are JSON booleans, prices/quantities are strings.
  * API key permissions: GET /sapi/v1/account/apiRestrictions (spot host) -> enableWithdrawals / enableFutures.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..exchange.filters import SymbolFilters, fmt, parse_exchange_info
from ..exchange.rest import BinanceAPIError, NetworkError, RestClient
from ..risk.liquidation import parse_brackets
from .base import Account, ExecutionError, ExitSummary, Fill, Position, ProtectiveOrder

log = logging.getLogger("imperion.exec.binance")

CLOSE_SIDE = {"LONG": "SELL", "SHORT": "BUY"}
ENTRY_SIDE = {"LONG": "BUY", "SHORT": "SELL"}


class BinanceExecutor:
    def __init__(self, cfg: Settings, trade: RestClient, spot: Optional[RestClient], filters: Dict[str, SymbolFilters]):
        self.cfg, self.rest, self.spot = cfg, trade, spot
        self._filters = filters
        self.mode = cfg.mode
        self._prepared: Dict[str, bool] = {}
        self._brackets: Dict[str, Any] = {}           # symbol -> (fetched_at, brackets)

    # ---- reference data ------------------------------------------------
    def filters(self, symbol: str) -> SymbolFilters:
        f = self._filters.get(symbol)
        if f is None:
            raise ExecutionError(f"no exchange filters for {symbol}")
        return f

    def refresh_filters(self) -> int:
        self._filters.update(parse_exchange_info(self.rest.request("GET", "/fapi/v1/exchangeInfo")))
        return len(self._filters)

    def leverage_brackets(self, symbol: str, max_age_s: float = 3600.0) -> List[Dict[str, float]]:
        """Maintenance-margin brackets of `symbol` (GET /fapi/v1/leverageBracket), cached for an hour. Raises on failure."""
        hit = self._brackets.get(symbol)
        if hit and time.time() - hit[0] < max_age_s:
            return hit[1]
        data = self.rest.request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True, priority=True)
        brackets = parse_brackets(data, symbol)
        self._brackets[symbol] = (time.time(), brackets)
        return brackets

    # ---- account -------------------------------------------------------
    def _get_versioned(self, name: str) -> Any:
        try:
            return self.rest.request("GET", f"/fapi/v3/{name}", signed=True)
        except BinanceAPIError as exc:
            if exc.status == 404:
                return self.rest.request("GET", f"/fapi/v2/{name}", signed=True)
            raise

    def account(self) -> Account:
        d = self._get_versioned("account")
        wallet = float(d.get("totalWalletBalance", 0) or 0)
        upnl = float(d.get("totalUnrealizedProfit", 0) or 0)
        equity = float(d["totalMarginBalance"]) if d.get("totalMarginBalance") not in (None, "") else wallet + upnl
        return Account(equity=equity, available=float(d.get("availableBalance", 0) or 0), unrealized=upnl)

    def positions(self) -> Dict[str, Position]:
        rows = self._get_versioned("positionRisk")
        if not isinstance(rows, list):       # never read a malformed answer as "no positions": that would mark live trades as closed
            raise ExecutionError(f"unexpected positionRisk payload: {str(rows)[:120]}")
        out: Dict[str, Position] = {}
        for r in rows:
            qty = float(r.get("positionAmt", 0) or 0)
            if qty == 0:
                continue
            out[r["symbol"]] = Position(
                r["symbol"], qty, float(r.get("entryPrice", 0) or 0),
                float(r.get("markPrice", 0) or 0),
                float(r.get("unRealizedProfit", r.get("unrealizedProfit", 0)) or 0),
                float(r.get("liquidationPrice", 0) or 0),
            )
        return out

    # ---- preflight -----------------------------------------------------
    def preflight(self) -> Dict[str, Any]:
        """Hard safety checks before any order may be sent. Returns {ok, checks, errors, warnings}."""
        checks: Dict[str, Any] = {}
        errors: List[str] = []
        warnings: List[str] = []
        try:
            off = self.rest.sync_time()
            checks["clock_offset_ms"] = off
            if abs(off) > 2000:
                errors.append(f"clock offset {off} ms is too large")
        except Exception as exc:
            errors.append(f"time sync failed: {exc}")

        if self.cfg.mode == "live":
            if self.spot is None:
                errors.append("spot client missing for apiRestrictions check")
            else:
                try:
                    r = self.spot.request("GET", "/sapi/v1/account/apiRestrictions", signed=True, priority=True)
                    checks["api_restrictions"] = {k: r.get(k) for k in ("enableWithdrawals", "enableFutures", "enableReading", "ipRestrict")}
                    if r.get("enableWithdrawals"):
                        errors.append("API key has WITHDRAWAL permission enabled. Create a key WITHOUT withdrawals. Refusing to trade.")
                    if not r.get("enableFutures"):
                        errors.append("API key does not have Futures trading enabled")
                    if not r.get("ipRestrict"):
                        warnings.append("API key is not IP-restricted (recommended: restrict to your server IP)")
                except Exception as exc:
                    msg = f"could not verify API key permissions: {exc}"
                    (warnings if self.cfg.allow_unverified_key_permissions else errors).append(msg)

        try:
            acct = self.account()
            checks["equity"], checks["available"] = round(acct.equity, 4), round(acct.available, 4)
            if acct.equity < self.cfg.min_equity_usdt:
                errors.append(f"account equity {acct.equity:.2f} < MIN_EQUITY_USDT {self.cfg.min_equity_usdt}")
        except Exception as exc:
            errors.append(f"account query failed: {exc}")

        try:
            dual = self.rest.request("GET", "/fapi/v1/positionSide/dual", signed=True)
            checks["hedge_mode"] = bool(dual.get("dualSidePosition"))
            if dual.get("dualSidePosition"):
                errors.append("Hedge (dual-side) position mode is on; switch the account to One-way mode")
        except Exception as exc:
            errors.append(f"position mode query failed: {exc}")
        try:
            multi = self.rest.request("GET", "/fapi/v1/multiAssetsMargin", signed=True)
            checks["multi_assets"] = bool(multi.get("multiAssetsMargin"))
            if multi.get("multiAssetsMargin"):
                errors.append("Multi-Assets mode is on; isolated margin needs Single-Asset mode")
        except Exception as exc:
            # Account-mode uncertainty is not safe to downgrade to a warning:
            # isolated-margin execution requires positively verified Single-Asset mode.
            errors.append(f"multi-assets mode query failed; refusing execution: {exc}")

        try:
            n = self.refresh_filters()
            missing = [s for s in self.cfg.symbols if s not in self._filters or self._filters[s].status != "TRADING"]
            checks["symbols_loaded"] = n
            if missing:
                errors.append(f"symbols not tradable on this venue: {missing}")
        except Exception as exc:
            errors.append(f"exchangeInfo failed: {exc}")
        return {"ok": not errors, "checks": checks, "errors": errors, "warnings": warnings}

    # ---- order plumbing --------------------------------------------------
    def prepare(self, symbol: str) -> None:
        if self._prepared.get(symbol):
            return
        try:
            self.rest.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True, priority=True)
        except BinanceAPIError as exc:
            if exc.code != -4046:  # "No need to change margin type"
                raise ExecutionError(f"set margin type failed: {exc}") from exc
        try:
            self.rest.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": self.cfg.leverage}, signed=True, priority=True)
        except BinanceAPIError as exc:
            raise ExecutionError(f"set leverage failed: {exc}") from exc
        self._prepared[symbol] = True

    def _order(self, symbol: str, side: str, qty: Decimal, client_id: str, reduce_only: bool) -> Fill:
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": fmt(qty),
                  "newClientOrderId": client_id, "newOrderRespType": "RESULT"}
        if reduce_only:
            params["reduceOnly"] = "true"
        try:
            data = self.rest.request("POST", "/fapi/v1/order", params, signed=True, priority=True, retries=0)
        except NetworkError:
            data = self._recover_order(symbol, client_id)
        return self._to_fill(symbol, client_id, data)

    def _recover_order(self, symbol: str, client_id: str) -> Dict[str, Any]:
        """After a timeout the order may or may not exist: look it up by our client id."""
        for _ in range(3):
            time.sleep(0.5)
            try:
                return self.rest.request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True, priority=True)
            except BinanceAPIError as exc:
                if exc.code == -2013:  # order does not exist
                    break
            except NetworkError:
                continue
        raise ExecutionError(f"order {client_id} outcome unknown after network error")

    def _to_fill(self, symbol: str, client_id: str, data: Dict[str, Any]) -> Fill:
        for _ in range(6):
            status = data.get("status", "")
            if status in ("FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED", "REJECTED"):
                break
            time.sleep(0.3)
            data = self.rest.request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True, priority=True)
        qty = Decimal(str(data.get("executedQty", "0") or "0"))
        avg = float(data.get("avgPrice", 0) or 0)
        if avg <= 0 and qty > 0 and float(data.get("cumQuote", 0) or 0) > 0:
            avg = float(data["cumQuote"]) / float(qty)
        return Fill(qty=qty, avg_price=avg, order_id=str(data.get("orderId", "")), status=data.get("status", ""))

    def enter(self, symbol: str, side: str, qty: Decimal, client_id: str) -> Fill:
        if self.cfg.entry_mode == "maker":
            fill = self._enter_maker(symbol, side, qty, client_id)
            if fill is not None and fill.qty > 0:
                return fill                       # full or partial maker fill: the lifecycle protects what was filled
            if self.cfg.entry_maker_fallback == "skip":
                return Fill(Decimal(0), 0.0, "", "MAKER_NOT_FILLED")
            client_id += "m"                      # nothing filled and the limit order is gone: take liquidity instead
        return self._order(symbol, ENTRY_SIDE[side], qty, client_id, reduce_only=False)

    def _enter_maker(self, symbol: str, side: str, qty: Decimal, client_id: str) -> Optional[Fill]:
        """Post-only (GTX) limit at the touch. Returns the fill (possibly partial / zero) or None if it was rejected.

        The order is always cancelled before returning unless it filled completely, so no resting entry order can
        open a position later behind the lifecycle's back.
        """
        f = self.filters(symbol)
        book = self.rest.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}, priority=True)
        long_ = side == "LONG"
        touch = float(book["bidPrice"] if long_ else book["askPrice"])
        if touch <= 0:
            return None
        price = f.round_price(touch, "down" if long_ else "up")
        if not f.qty_ok(qty, market=False):
            return None
        params = {"symbol": symbol, "side": ENTRY_SIDE[side], "type": "LIMIT", "timeInForce": "GTX", "price": fmt(price),
                  "quantity": fmt(qty), "newClientOrderId": client_id, "newOrderRespType": "RESULT"}
        lookup = {"symbol": symbol, "origClientOrderId": client_id}
        try:
            data = self.rest.request("POST", "/fapi/v1/order", params, signed=True, priority=True, retries=0)
        except BinanceAPIError as exc:
            if exc.code == -5022:                 # post-only order would have matched immediately
                return None
            raise
        except NetworkError:
            data = self._recover_order(symbol, client_id)
        deadline = time.time() + self.cfg.entry_maker_wait_s
        while data.get("status") in ("NEW", "PARTIALLY_FILLED") and time.time() < deadline:
            time.sleep(0.25)
            data = self.rest.request("GET", "/fapi/v1/order", lookup, signed=True, priority=True)
        if data.get("status") in ("NEW", "PARTIALLY_FILLED"):
            try:
                self.rest.request("DELETE", "/fapi/v1/order", lookup, signed=True, priority=True, retries=0)
            except BinanceAPIError as exc:
                if exc.code not in (-2011, -2013):    # already filled / gone
                    raise
            data = self.rest.request("GET", "/fapi/v1/order", lookup, signed=True, priority=True)
            if data.get("status") in ("NEW", "PARTIALLY_FILLED"):
                raise ExecutionError(f"maker entry {client_id} could not be cancelled (status {data.get('status')})")
        qty_done = Decimal(str(data.get("executedQty", "0") or "0"))
        avg = float(data.get("avgPrice", 0) or 0) or float(price)
        return Fill(qty=qty_done, avg_price=avg if qty_done > 0 else 0.0, order_id=str(data.get("orderId", "")), status=str(data.get("status", "")))

    def close_market(self, symbol: str, side: str, qty: Decimal, client_id: str) -> Fill:
        """`side` is the side of the POSITION being closed (LONG/SHORT)."""
        return self._order(symbol, CLOSE_SIDE[side], qty, client_id, reduce_only=True)

    def _algo(self, params: Dict[str, Any]) -> str:
        base = {"algoType": "CONDITIONAL", **params}
        data = self.rest.request("POST", "/fapi/v1/algoOrder", base, signed=True, priority=True, retries=0)
        algo_id = data.get("algoId")
        if algo_id is None:
            raise ExecutionError(f"algo order response without algoId: {data}")
        return str(algo_id)

    def place_stop(self, symbol: str, side: str, trigger: float, qty: Optional[Decimal], client_id: str) -> str:
        """Protective stop for a position of `side`. Prefers closePosition; falls back to quantity+reduceOnly."""
        f = self.filters(symbol)
        mode = "down" if side == "LONG" else "up"   # round away from entry (never tighter than planned)
        trig = fmt(f.round_price(trigger, mode))
        base = {"symbol": symbol, "side": CLOSE_SIDE[side], "type": "STOP_MARKET", "triggerPrice": trig,
                "workingType": "MARK_PRICE", "clientAlgoId": client_id}
        try:
            return self._algo({**base, "closePosition": "true"})
        except BinanceAPIError as exc:
            if qty is None:
                raise ExecutionError(f"stop (closePosition) rejected: {exc}") from exc
            log.warning("closePosition stop rejected (%s); retrying with quantity+reduceOnly", exc)
            try:
                return self._algo({**base, "quantity": fmt(qty), "reduceOnly": "true", "clientAlgoId": client_id[:34] + "r"})
            except BinanceAPIError as exc2:
                raise ExecutionError(f"stop rejected: {exc2}") from exc2

    def place_tp(self, symbol: str, side: str, trigger: float, qty: Decimal, client_id: str) -> str:
        f = self.filters(symbol)
        mode = "down" if side == "LONG" else "up"   # closer to price than planned: slightly easier to fill
        trig = fmt(f.round_price(trigger, mode))
        try:
            return self._algo({"symbol": symbol, "side": CLOSE_SIDE[side], "type": "TAKE_PROFIT_MARKET", "triggerPrice": trig,
                               "quantity": fmt(qty), "reduceOnly": "true", "workingType": "CONTRACT_PRICE", "clientAlgoId": client_id})
        except BinanceAPIError as exc:
            raise ExecutionError(f"take-profit rejected: {exc}") from exc

    def protective_orders(self, symbol: str) -> List[ProtectiveOrder]:
        data = self.rest.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True, priority=True)
        rows = data.get("orders", data) if isinstance(data, dict) else data
        out: List[ProtectiveOrder] = []
        for r in rows or []:
            typ = str(r.get("orderType", r.get("type", "")))
            kind = "STOP" if typ.startswith("STOP") else "TP" if typ.startswith("TAKE_PROFIT") else None
            if not kind:
                continue
            out.append(ProtectiveOrder(str(r.get("algoId")), kind, float(r.get("triggerPrice", r.get("stopPrice", 0)) or 0), float(r.get("quantity", r.get("origQty", 0)) or 0),
                                       str(r.get("closePosition", "false")).lower() == "true", str(r.get("clientAlgoId", ""))))
        return out

    def cancel_protective(self, symbol: str, order_id: str) -> None:
        try:
            self.rest.request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": order_id}, signed=True, priority=True, retries=0)
        except BinanceAPIError as exc:
            if exc.code not in (-2011, -2013):  # unknown/already gone
                raise

    def cancel_all(self, symbol: str) -> None:
        for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/allOpenOrders"):
            try:
                self.rest.request("DELETE", path, {"symbol": symbol}, signed=True, priority=True, retries=0)
            except BinanceAPIError as exc:
                if exc.code not in (-2011, -2013):
                    raise

    def exit_summary(self, symbol: str, since_ms: int, entry_side: str) -> ExitSummary:
        """Aggregate this trade's fills. userTrades accepts at most a 7-day window, so long (swing) holds are fetched in slices."""
        exit_side = CLOSE_SIDE[entry_side]
        q = pq = realized = fees = 0.0
        seen = set()
        start, now = since_ms, int(time.time() * 1000) + 60_000
        while start < now:
            end = min(start + 6 * 86_400_000, now)
            rows = self.rest.request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start, "endTime": end, "limit": 1000},
                                     signed=True, priority=True)
            for r in rows or []:
                key = r.get("id", (r.get("time"), r.get("price"), r.get("qty"), r.get("side")))
                if key in seen:
                    continue
                seen.add(key)
                commission = float(r.get("commission", 0) or 0)
                if r.get("commissionAsset", "USDT") not in ("USDT", "USDC", "BUSD"):
                    commission = float(r["price"]) * float(r["qty"]) * self.cfg.taker_fee_pct / 100  # fee paid in BNB: estimate
                fees += commission
                realized += float(r.get("realizedPnl", 0) or 0)
                if r.get("side") == exit_side:
                    q += float(r["qty"]); pq += float(r["price"]) * float(r["qty"])
            start = end
        return ExitSummary(qty=q, avg_exit=pq / q if q else 0.0, realized=realized, fees=fees, complete=q > 0,
                           funding=self._funding(symbol, since_ms) if q > 0 else 0.0)

    def _funding(self, symbol: str, since_ms: int) -> float:
        """Signed funding income since the entry (negative = paid). Best effort: a failure never blocks finalisation."""
        try:
            rows = self.rest.request("GET", "/fapi/v1/income", {"symbol": symbol, "incomeType": "FUNDING_FEE", "startTime": since_ms, "limit": 1000},
                                     signed=True, priority=True)
            return sum(float(r.get("income", 0) or 0) for r in rows) if isinstance(rows, list) else 0.0
        except Exception as exc:
            log.warning("funding income query failed for %s: %s", symbol, exc)
            return 0.0
