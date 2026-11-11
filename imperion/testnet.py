"""Binance USDⓈ-M Futures Demo (Testnet) validation and signed gate evidence.

Read-only by default: clock, exchangeInfo, ticker, signed account / positions / open orders / leverage brackets,
ONE-WAY position mode (and Single-Asset margin, which isolated margin needs), market WebSocket and the private
user-stream handshake. The bounded ORDER LIFECYCLE (entry -> exchange-confirmed protective stop -> reduce-only close ->
cleanup) runs only with TESTNET_ORDER_SMOKE=true and TESTNET_ONLY_ACK=I_ACCEPT_TESTNET_ORDERS. While that position is
open, the EXCHANGE LIQUIDATION check reads the real liquidationPrice of the position and verifies that
  * it exists (> 0) and the stop sits between entry and liquidation,
  * the stop triggers within LIQUIDATION_SAFETY (70 %) of the way to liquidation, and
  * our own bracket-based liquidation model (risk/liquidation.py) is not more optimistic than the exchange by more than
    LIQ_MODEL_TOLERANCE of the entry price (the risk manager sizes every live trade with that model).
A passed run that included every check is signed with HMAC-SHA256 (TESTNET_EVIDENCE_SECRET), stored as evidence for
the Testnet -> Live gate (imperion/gate.py) and written to TESTNET_EVIDENCE_OUTPUT for the GitHub artifact.
The command refuses production endpoints.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .config import VERSION, Settings, load_settings
from .exchange.rest import RestClient
from .execution.binance import BinanceExecutor
from .risk.liquidation import assess
from .risk.manager import LIQUIDATION_SAFETY
from .util import now_ms

TESTNET_ACK = "I_ACCEPT_TESTNET_ORDERS"
LIQ_MODEL_TOLERANCE = 0.005          # model may not place liquidation further away than the exchange by > 0.5 % of entry
SMOKE_STOP_FRACTION = 0.98           # smoke-test stop: 2 % below the long entry
DEMO_HOSTS = {"BINANCE_TRADE_BASE": "demo-fapi.binance.com", "BINANCE_REST_BASE": "demo-fapi.binance.com", "BINANCE_WS_BASE": "demo-fstream.binance.com"}


def ws_handshake(url: str, *, receive_message: bool) -> Optional[dict]:
    import websocket

    sock = websocket.create_connection(url, timeout=12, enable_multithread=True)
    try:
        if not receive_message:
            return None
        data = json.loads(sock.recv())
        if not isinstance(data, dict):
            raise RuntimeError("WebSocket returned a non-object payload")
        return data
    finally:
        sock.close()


class TestnetValidator:
    __test__ = False                               # not a pytest test class

    def __init__(self, cfg: Settings, rest: RestClient, ws_check: Callable[..., Optional[dict]] = ws_handshake,
                 out: Callable[[str], None] = print):
        self.cfg, self.rest, self.ws_check, self.out = cfg, rest, ws_check, out
        self.checks: List[Dict[str, Any]] = []
        self.position_mode: Optional[str] = None
        self.liquidation: Optional[Dict[str, Any]] = None

    def _check(self, name: str, fn: Callable[[], str]) -> bool:
        try:
            detail = fn() or ""
            self.checks.append({"name": name, "ok": True, "detail": detail})
            self.out(f"PASS {name} {detail}".rstrip())
            return True
        except Exception as exc:
            self.checks.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
            self.out(f"FAIL {name} {type(exc).__name__}: {exc}")
            return False

    def run(self, symbol: str, order_smoke: bool = False, qty: Optional[Decimal] = None, max_notional: float = 100.0) -> Dict[str, Any]:
        rest, cfg = self.rest, self.cfg

        def clock() -> str:
            off = rest.sync_time()
            if abs(off) > 5000:
                raise RuntimeError(f"clock offset {off} ms")
            return f"offset_ms={off}"

        def market() -> str:
            info = {r.get("symbol"): r for r in rest.request("GET", "/fapi/v1/exchangeInfo").get("symbols", [])}
            if info.get(symbol, {}).get("status") != "TRADING":
                raise RuntimeError(f"{symbol} is not TRADING on Demo Futures")
            px = float(rest.request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}).get("price", 0) or 0)
            if px <= 0:
                raise RuntimeError("no ticker price")
            return f"{symbol} price={px}"

        def signed() -> str:
            acct = BinanceExecutor(cfg, rest, None, {})._get_versioned("account")
            if not isinstance(acct, dict):
                raise RuntimeError("malformed account response")
            pos = BinanceExecutor(cfg, rest, None, {})._get_versioned("positionRisk")
            if not isinstance(pos, list):
                raise RuntimeError("malformed positionRisk response")
            rest.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
            rest.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
            BinanceExecutor(cfg, rest, None, {}).leverage_brackets(symbol)
            return f"positions={len(pos)} brackets=ok"

        def position_mode() -> str:
            dual = rest.request("GET", "/fapi/v1/positionSide/dual", signed=True)
            if not isinstance(dual, dict) or "dualSidePosition" not in dual:
                raise RuntimeError("malformed positionSide/dual response")
            if dual.get("dualSidePosition"):
                self.position_mode = "HEDGE"
                raise RuntimeError("Hedge (dual-side) position mode is on; switch the Demo account to One-way mode")
            multi = rest.request("GET", "/fapi/v1/multiAssetsMargin", signed=True)
            if isinstance(multi, dict) and multi.get("multiAssetsMargin"):
                raise RuntimeError("Multi-Assets margin mode is on; isolated margin needs Single-Asset mode")
            self.position_mode = "ONE_WAY"
            return "mode=ONE_WAY multi_assets=false"

        def market_ws() -> str:
            if not self.ws_check(f"{cfg.ws_base}/market/stream?streams={symbol.lower()}@aggTrade", receive_message=True):
                raise RuntimeError("market WebSocket returned no payload")
            return "first_event=true"

        def user_ws() -> str:
            key = str(rest.request("POST", "/fapi/v1/listenKey", priority=True, retries=0).get("listenKey") or "")
            if not key:
                raise RuntimeError("listenKey was not issued")
            try:
                self.ws_check(f"{cfg.ws_base}/{cfg.user_stream_path}/{key}", receive_message=False)
            finally:
                try:
                    rest.request("DELETE", "/fapi/v1/listenKey", priority=True, retries=0)
                except Exception:
                    pass
            return f"path=/{cfg.user_stream_path}/<listenKey>"

        ok = all([self._check("clock", clock), self._check("market_data", market), self._check("signed_rest", signed),
                  self._check("position_mode", position_mode), self._check("market_websocket", market_ws),
                  self._check("user_stream_handshake", user_ws)])
        lifecycle = False
        if order_smoke:
            if self.position_mode != "ONE_WAY":
                self._record("order_lifecycle", False, "not attempted: One-way position mode is required")
                self._record("exchange_liquidation", False, "not attempted")
                ok = False
            else:
                lifecycle = self._check("order_lifecycle", lambda: self._order_smoke(symbol, qty, max_notional))
                liq = self.liquidation or {"ok": False, "detail": "not reached (the order lifecycle stopped before the position was open)"}
                self._record("exchange_liquidation", bool(liq.get("ok")), liq.get("detail", ""))
                ok = ok and lifecycle and bool(liq.get("ok"))
        else:
            self.out("SKIP order_lifecycle + exchange_liquidation (read-only; TESTNET_ORDER_SMOKE=true + TESTNET_ONLY_ACK enable them)")
        return {"passed": ok, "order_lifecycle": lifecycle, "symbol": symbol, "ts": now_ms(), "version": VERSION,
                "host": urlparse(cfg.trade_base).hostname, "position_mode": self.position_mode,
                "liquidation": self.liquidation, "checks": self.checks}

    def _record(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        self.out(f"{'PASS' if ok else 'FAIL'} {name} {detail}".rstrip())

    def _liquidation_check(self, ex: BinanceExecutor, symbol: str, entry: float, stop: float, qty: float) -> Dict[str, Any]:
        """Real exchange liquidationPrice of the open smoke position vs the stop and vs our own model. Never raises."""
        res: Dict[str, Any] = {"ok": False, "side": "LONG", "entry": entry, "stop": stop, "qty": qty, "leverage": self.cfg.leverage,
                               "safety": LIQUIDATION_SAFETY, "tolerance": LIQ_MODEL_TOLERANCE}
        try:
            pos = ex.positions().get(symbol)
            liq = float(getattr(pos, "liq_price", 0) or 0) if pos is not None else 0.0
            res["exchange_liq_price"] = liq
            if pos is None or liq <= 0:
                raise RuntimeError("exchange liquidationPrice missing for the open position")
            if not 0 < liq < stop < entry:
                raise RuntimeError(f"stop {stop:.6g} is not between liquidation {liq:.6g} and entry {entry:.6g}")
            ratio = (entry - stop) / (entry - liq)
            res["stop_to_liq"] = round(ratio, 4)
            if ratio >= LIQUIDATION_SAFETY:
                raise RuntimeError(f"stop uses {ratio:.0%} of the distance to the exchange liquidation (max {LIQUIDATION_SAFETY:.0%})")
            model = assess("LONG", entry, stop, qty, self.cfg.leverage, ex.leverage_brackets(symbol), LIQUIDATION_SAFETY)
            res["model_liq_price"] = model["liq_price"]
            optimism = (liq - model["liq_price"]) / entry          # > 0: model thinks liquidation is further away than it is
            res["model_optimism_pct"] = round(optimism * 100, 4)
            if optimism > LIQ_MODEL_TOLERANCE:
                raise RuntimeError(f"liquidation model is optimistic by {optimism:.2%} of entry (exchange {liq:.6g}, model "
                                   f"{model['liq_price']:.6g}); the risk manager would size positions too close to liquidation")
            res["ok"] = True
            res["detail"] = (f"exchange_liq={liq:.6g} model_liq={model['liq_price']:.6g} stop_to_liq={ratio:.2f} "
                             f"model_optimism={optimism:+.3%}")
        except Exception as exc:
            res["detail"] = f"{type(exc).__name__}: {exc}"
        return res

    def _order_smoke(self, symbol: str, qty: Optional[Decimal], max_notional: float) -> str:
        if qty is None or qty <= 0:
            raise RuntimeError("TESTNET_ORDER_QTY must be a positive quantity")
        ex = BinanceExecutor(self.cfg, self.rest, None, {})
        ex.refresh_filters()
        f = ex.filters(symbol)
        if f.status != "TRADING" or not f.qty_ok(qty) or f.floor_qty(float(qty)) != qty:
            raise RuntimeError("TESTNET_ORDER_QTY violates the market lot-size filters")
        mark = float(self.rest.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}).get("markPrice", 0) or 0)
        notional = Decimal(str(mark)) * qty
        if mark <= 0 or notional < f.min_notional or notional > Decimal(str(max_notional)):
            raise RuntimeError(f"order notional {notional:.2f} must be within [{f.min_notional}, {max_notional}]")
        if symbol in ex.positions() or self.rest.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True) or ex.protective_orders(symbol):
            raise RuntimeError(f"refusing: {symbol} already has a position or open orders on this account")
        ex.prepare(symbol)
        tag = "imp" + uuid.uuid4().hex[:10]
        try:
            fill = ex.enter(symbol, "LONG", qty, tag + "e")
            if fill.qty <= 0:
                raise RuntimeError(f"entry did not fill ({fill.status})")
            stop_px = fill.avg_price * SMOKE_STOP_FRACTION
            stop_id = ex.place_stop(symbol, "LONG", stop_px, fill.qty, tag + "s")
            if not any(o.kind == "STOP" and o.order_id == stop_id for o in ex.protective_orders(symbol)):
                raise RuntimeError("exchange did not confirm the protective stop")
            self.liquidation = self._liquidation_check(ex, symbol, fill.avg_price, stop_px, float(fill.qty))
            liq = self.liquidation.get("exchange_liq_price", 0.0)
            closed = ex.close_market(symbol, "LONG", fill.qty, tag + "c")
            if closed.qty <= 0:
                raise RuntimeError("reduce-only close did not fill")
            time.sleep(0.5)
            if symbol in ex.positions():
                raise RuntimeError("position still open after the close")
            ex.cancel_all(symbol)
            if ex.protective_orders(symbol):
                raise RuntimeError("conditional orders remain after cleanup")
            return f"qty={fill.qty} stop_confirmed=true liquidation_price={liq} cleanup=true"
        finally:
            try:
                left = ex.positions().get(symbol)
                if left and left.qty:
                    ex.close_market(symbol, left.side, abs(Decimal(str(left.qty))), tag + "x")
            finally:
                ex.cancel_all(symbol)


def main() -> int:
    from .db import Database
    from .gate import MIN_SECRET_LEN, record_testnet_evidence, write_evidence_file
    try:
        cfg = load_settings()
        if cfg.mode != "testnet":
            raise RuntimeError("set MODE=testnet; this validator never runs against production")
        if not cfg.has_credentials:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET (Demo Futures keys) are required")
        for label, value in (("BINANCE_TRADE_BASE", cfg.trade_base), ("BINANCE_REST_BASE", cfg.rest_base), ("BINANCE_WS_BASE", cfg.ws_base)):
            if urlparse(value).hostname != DEMO_HOSTS[label]:
                raise RuntimeError(f"{label} must point to {DEMO_HOSTS[label]}, got {urlparse(value).hostname!r}")
        smoke = os.getenv("TESTNET_ORDER_SMOKE", "false").lower() == "true"
        if smoke and os.getenv("TESTNET_ONLY_ACK", "") != TESTNET_ACK:
            raise RuntimeError(f"the order smoke test needs TESTNET_ONLY_ACK={TESTNET_ACK}")
        if smoke and len(cfg.testnet_evidence_secret) < MIN_SECRET_LEN:
            raise RuntimeError(f"the order smoke test produces gate evidence: set TESTNET_EVIDENCE_SECRET (>= {MIN_SECRET_LEN} chars) first")
        qty_raw = os.getenv("TESTNET_ORDER_QTY", "").strip()
        rest = RestClient(cfg.trade_base, cfg.api_key, cfg.api_secret, name="testnet")
        report = TestnetValidator(cfg, rest, ws_handshake).run(os.getenv("TESTNET_SYMBOL", "BTCUSDT").upper(), smoke, Decimal(qty_raw) if qty_raw else None,
                                                 float(os.getenv("TESTNET_MAX_NOTIONAL_USDT", "100")))
    except Exception as exc:
        print(f"TESTNET_VALIDATION=FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    ev = record_testnet_evidence(Database(cfg.db_path), report, cfg.testnet_evidence_secret)
    print("TESTNET_VALIDATION=" + ("PASS" if report["passed"] else "FAIL"))
    if ev:
        out_path = os.getenv("TESTNET_EVIDENCE_OUTPUT", "").strip() or os.path.join(cfg.storage_dir, "testnet-evidence.json")
        digest = write_evidence_file(ev, out_path)
        print(f"signed gate evidence recorded and written to {out_path}")
        print(f"EVIDENCE_SHA256={digest}")
        if not ev["order_lifecycle"]:
            print("read-only evidence: NOT sufficient for the live gate (needs the order lifecycle + exchange liquidation checks)")
    elif report["passed"]:
        print("no gate evidence recorded: TESTNET_EVIDENCE_SECRET is not set")
    return 0 if report["passed"] else 1
