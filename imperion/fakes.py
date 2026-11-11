"""In-process fake of the Binance USDⓈ-M REST API, used by the test-suite and `python -m imperion selftest`.

It is deliberately STRICT so that integration bugs show up here instead of with real money:
  * every signed request must carry a valid HMAC-SHA256 signature, the API-key header and a fresh timestamp;
  * conditional order types on /fapi/v1/order are rejected with -4120 (they belong to the Algo Service);
  * /fapi/v1/algoOrder validates algoType / triggerPrice / closePosition-vs-quantity / trigger side;
  * quantities must respect step size and min notional, reduceOnly needs a position.
Market data is a deterministic synthetic price path (not real prices) so every run is reproducible.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import threading
import weakref
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

from .exchange.rest import NetworkError
from .util import TF_SECONDS, candle_open_for, now_ms

BASE_PRICES = {"BTCUSDT": 90000.0, "ETHUSDT": 3100.0, "BNBUSDT": 650.0, "SOLUSDT": 140.0,
               "XRPUSDT": 2.2, "ADAUSDT": 0.7, "DOGEUSDT": 0.2, "TRXUSDT": 0.25,
               "AVAXUSDT": 18.0, "LINKUSDT": 15.0, "DOTUSDT": 3.0, "LTCUSDT": 85.0,
               "BCHUSDT": 350.0, "SUIUSDT": 2.8, "NEARUSDT": 5.0, "APTUSDT": 8.0}
# (tick, step, min_notional) — deterministic test metadata for the full fixed universe.
SYMBOL_SPECS = {
    "BTCUSDT": ("0.1", "0.001", "100"), "ETHUSDT": ("0.01", "0.001", "20"),
    "BNBUSDT": ("0.01", "0.001", "5"), "SOLUSDT": ("0.01", "0.01", "5"),
    "XRPUSDT": ("0.0001", "0.1", "5"), "ADAUSDT": ("0.0001", "1", "5"),
    "DOGEUSDT": ("0.00001", "1", "5"), "TRXUSDT": ("0.00001", "1", "5"),
    "AVAXUSDT": ("0.001", "0.1", "5"), "LINKUSDT": ("0.001", "0.01", "5"),
    "DOTUSDT": ("0.0001", "0.1", "5"), "LTCUSDT": ("0.01", "0.001", "20"),
    "BCHUSDT": ("0.01", "0.001", "5"), "SUIUSDT": ("0.0001", "0.1", "5"),
    "NEARUSDT": ("0.001", "0.1", "5"), "APTUSDT": ("0.001", "0.1", "5"),
}
EXTRA_RADAR = {"HYPEUSDT": 30.0, "INJUSDT": 7.0, "FILUSDT": 2.0}



_CANDLE_CACHE: Dict[Tuple[int, str, str, int], list] = {}
_ENGINES: "weakref.WeakSet" = weakref.WeakSet()       # engines built by make_engine (test harness closes them)


TEST_EVIDENCE_SECRET = "test-harness-evidence-secret-0123456789abcdef"   # fake harness only, never a production value


def passed_testnet_report(ts: Optional[int] = None, **over: Any) -> Dict[str, Any]:
    """A complete, passed validator report (what a real `testnet-check` with the order smoke test returns)."""
    from .gate import REQUIRED_CHECKS
    from .util import now_ms
    rep = {"passed": True, "order_lifecycle": True, "symbol": "BTCUSDT", "host": "demo-fapi.binance.com", "ts": ts or now_ms(),
           "position_mode": "ONE_WAY", "checks": [{"name": n, "ok": True} for n in REQUIRED_CHECKS],
           "liquidation": {"ok": True, "exchange_liq_price": 47_000.0, "model_liq_price": 46_990.0, "stop_to_liq": 0.33}}
    rep.update(over)
    return rep


def seed_live_gate(db, secret: str = TEST_EVIDENCE_SECRET) -> None:
    from .gate import APPROVAL_PHRASE, approve_live, record_testnet_evidence
    record_testnet_evidence(db, passed_testnet_report(), secret)
    approve_live(db, "test-harness", APPROVAL_PHRASE, 30, secret)


def stop_all_engines() -> List[str]:
    """Stop every engine created by make_engine that is still alive; returns threads that refused to stop."""
    stuck: List[str] = []
    for engine in list(_ENGINES):
        stuck += engine.stop(timeout=10.0)
    return stuck


def _h(*parts: Any) -> float:
    """Deterministic pseudo-random in [-1, 1] from arbitrary parts."""
    d = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return int.from_bytes(d, "big") / 2**63 - 1.0


class FakeBinance:
    def __init__(self, symbols=tuple(BASE_PRICES), api_key: str = "TESTKEY", api_secret: str = "TESTSECRET", balance: float = 100.0,
                 withdrawals: bool = False, futures: bool = True, ip_restrict: bool = True, seed: int = 1, leverage_default: int = 3):
        self.symbols = list(symbols)
        self.api_key, self.api_secret = api_key, api_secret
        self.balance = balance
        self.withdrawals, self.futures, self.ip_restrict = withdrawals, futures, ip_restrict
        self.seed = seed
        self.override: Dict[str, float] = {}
        self.pos: Dict[str, Dict[str, float]] = {}
        self.algos: List[Dict[str, Any]] = []
        self.client_orders: Dict[str, Dict[str, Any]] = {}
        self.user_trades: List[Dict[str, Any]] = []
        self.calls: List[Tuple[str, str, Dict[str, str]]] = []
        self.leverage: Dict[str, int] = {}
        self.dual, self.multi_assets = False, False
        self.rules: List[Dict[str, Any]] = []
        self._id = 1000
        self._lock = threading.RLock()
        self.time_skew_ms = 0

    # ------------------------------------------------------------ price model
    # v10 additions are class-level defaults so existing constructor arguments stay untouched
    limit_mode = "fill"          # fill | partial | rest | reject
    bracket_single_object = False
    fill_lag_ms = 0              # exchange fill timestamps this far behind the local clock (server-time skew)

    @property
    def liq_override(self) -> Dict[str, float]:
        return self.__dict__.setdefault("_liq_override", {})

    @property
    def bracket_override(self) -> Dict[str, list]:
        return self.__dict__.setdefault("_bracket_override", {})
    listen_keys = 0

    @property
    def income(self) -> List[Dict[str, Any]]:
        return self.__dict__.setdefault("_income", [])

    def price(self, symbol: str, t_ms: Optional[int] = None) -> float:
        if t_ms is None and symbol in self.override:
            return self.override[symbol]
        t = (t_ms if t_ms is not None else now_ms()) / 1000.0
        base = BASE_PRICES.get(symbol) or EXTRA_RADAR.get(symbol, 10.0)
        s = self.seed + sum(map(ord, symbol))
        lp = (0.030 * math.sin(2 * math.pi * t / (3.1 * 86400) + s) + 0.012 * math.sin(2 * math.pi * t / (13 * 3600) + 2 * s)
              + 0.004 * math.sin(2 * math.pi * t / (47 * 60) + 3 * s) + 0.0006 * _h(symbol, int(t // 60), self.seed))
        return base * math.exp(lp)

    def set_price(self, symbol: str, price: float) -> None:
        """Force the market price and trigger any resting conditional orders."""
        with self._lock:
            self.override[symbol] = price
            self._trigger(symbol)

    def clear_price(self, symbol: str) -> None:
        self.override.pop(symbol, None)

    # ------------------------------------------------------------ failure injection
    def inject(self, method: str, path_part: str, *, times: int = 1, status: int = 400, code: int = -1000, msg: str = "injected failure",
               timeout: bool = False, execute_first: bool = False) -> None:
        self.rules.append({"method": method.upper(), "path": path_part, "times": times, "status": status, "code": code, "msg": msg,
                           "timeout": timeout, "execute_first": execute_first})

    # ------------------------------------------------------------ transport
    def transport(self, method: str, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, Dict[str, str], str]:
        u = urlparse(url)
        params = dict(parse_qsl(u.query, keep_blank_values=True))
        with self._lock:
            self.calls.append((method, u.path, params))
            for rule in self.rules:
                if rule["method"] == method and rule["path"] in u.path and rule["times"] > 0:
                    rule["times"] -= 1
                    if rule["timeout"]:
                        if rule["execute_first"]:
                            try:
                                self._dispatch(method, u.path, params, headers)
                            except _Err:
                                pass
                        raise NetworkError("timeout")
                    return rule["status"], {}, json.dumps({"code": rule["code"], "msg": rule["msg"]})
            try:
                status, body = self._dispatch(method, u.path, params, headers)
            except _Err as e:
                return e.status, {}, json.dumps({"code": e.code, "msg": e.msg})
        return status, {"x-mbx-used-weight-1m": "10"}, json.dumps(body)

    # ------------------------------------------------------------ dispatch
    def _check_signed(self, params: Dict[str, str], headers: Dict[str, str]) -> None:
        if headers.get("X-MBX-APIKEY") != self.api_key:
            raise _Err(401, -2015, "Invalid API-key, IP, or permissions for action.")
        sig = params.get("signature")
        if not sig:
            raise _Err(400, -1102, "Mandatory parameter 'signature' was not sent.")
        raw = "&".join(f"{k}={v}" for k, v in params.items() if k != "signature")
        expect = hmac.new(self.api_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            raise _Err(400, -1022, "Signature for this request is not valid.")
        ts = int(params.get("timestamp", "0"))
        if abs(now_ms() + self.time_skew_ms - ts) > int(params.get("recvWindow", "5000")):
            raise _Err(400, -1021, "Timestamp for this request is outside of the recvWindow.")

    def _dispatch(self, method: str, path: str, p: Dict[str, str], headers: Dict[str, str]) -> Tuple[int, Any]:
        # ---- public ----
        if path == "/fapi/v1/time":
            return 200, {"serverTime": now_ms() + self.time_skew_ms}
        if path == "/fapi/v1/exchangeInfo":
            return 200, self._exchange_info()
        if path == "/fapi/v1/klines":
            return 200, self._klines(p)
        if path == "/fapi/v1/ticker/24hr":
            return 200, [{"symbol": s, "quoteVolume": str(5e8 / (i + 1) + 1e7), "priceChangePercent": "1.2"} for i, s in enumerate(self.symbols + list(EXTRA_RADAR))]
        if path == "/fapi/v1/premiumIndex":
            rows = [{"symbol": s, "markPrice": str(self.price(s)), "lastFundingRate": str(0.0001 * _h(s, "f")), "nextFundingTime": now_ms() + 3600_000}
                    for s in self.symbols + list(EXTRA_RADAR)]
            if p.get("symbol"):                       # like the real API: a single object when a symbol is given
                return 200, next(r for r in rows if r["symbol"] == p["symbol"])
            return 200, rows
        if path == "/fapi/v1/ticker/price":
            return 200, {"symbol": p["symbol"], "price": str(self.price(p["symbol"]))}
        if path == "/fapi/v1/ticker/bookTicker":
            px = self.price(p["symbol"])
            return 200, {"symbol": p["symbol"], "bidPrice": str(px * 0.99995), "askPrice": str(px * 1.00005)}
        # ---- spot signed ----
        if path == "/sapi/v1/account/apiRestrictions":
            self._check_signed(p, headers)
            return 200, {"enableWithdrawals": self.withdrawals, "enableFutures": self.futures, "enableReading": True, "ipRestrict": self.ip_restrict}
        # ---- user data stream: API key only, no signature ----
        if path == "/fapi/v1/listenKey":
            if headers.get("X-MBX-APIKEY") != self.api_key:
                raise _Err(401, -2015, "Invalid API-key, IP, or permissions for action.")
            if method == "POST":
                self.listen_keys += 1
                return 200, {"listenKey": f"fake-listen-key-{self.listen_keys}"}
            return 200, {}
        # ---- futures signed ----
        if path.startswith("/fapi/"):
            self._check_signed(p, headers)
            return self._signed(method, path, p)
        raise _Err(404, -1, f"no route {path}")

    def _exchange_info(self) -> Dict[str, Any]:
        syms = []
        for s in self.symbols + list(EXTRA_RADAR):
            tick, step, notional = SYMBOL_SPECS.get(s, ("0.0001", "1", "5"))
            syms.append({"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": tick}, {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step, "maxQty": "100000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": step, "minQty": step, "maxQty": "50000"}, {"filterType": "MIN_NOTIONAL", "notional": notional}]})
        return {"symbols": syms}

    def _klines(self, p: Dict[str, str]) -> List[list]:
        sym, tf = p["symbol"], p["interval"]
        limit = int(p.get("limit", 500))
        step = TF_SECONDS[tf] * 1000
        now = now_ms()
        last_open = candle_open_for(now, tf)               # includes the forming candle, like the real API
        if "endTime" in p:
            last_open = min(last_open, candle_open_for(int(p["endTime"]), tf))
        first_open = last_open - (limit - 1) * step
        if "startTime" in p:
            first_open = max(first_open, candle_open_for(int(p["startTime"]), tf) + (0 if int(p["startTime"]) % step == 0 else step))
        rows = []
        t = first_open
        while t <= last_open and len(rows) < limit:
            rows.append(self._candle(sym, tf, t, step, now))
            t += step
        return rows

    def _candle(self, sym: str, tf: str, open_t: int, step: int, now: int) -> list:
        closed = open_t + step <= now
        key = (self.seed, sym, tf, open_t)
        if closed and key in _CANDLE_CACHE:              # closed candles are deterministic: computed once per process
            return _CANDLE_CACHE[key]
        row = self._make_candle(sym, tf, open_t, step, now)
        if closed:
            if len(_CANDLE_CACHE) > 400_000:
                _CANDLE_CACHE.clear()
            _CANDLE_CACHE[key] = row
        return row

    def _make_candle(self, sym: str, tf: str, open_t: int, step: int, now: int) -> list:
        n = 12 if step <= 3600_000 else 24
        end = min(open_t + step, now)
        pts = [self.price(sym, open_t + int((end - open_t) * i / (n - 1))) if end > open_t else self.price(sym, open_t) for i in range(n)]
        o, c, h, l = pts[0], pts[-1], max(pts), min(pts)
        h, l = h * (1 + 0.0004 * abs(_h(sym, tf, open_t, "h"))), l * (1 - 0.0004 * abs(_h(sym, tf, open_t, "l")))
        base_vol = (4e6 / BASE_PRICES.get(sym, 10.0)) * (step / 60000) ** 0.9 * (1 + 0.5 * _h(sym, tf, open_t, "v"))
        frac = (end - open_t) / step
        vol = max(base_vol * frac, 0.001)
        move = (c - o) / o if o else 0
        buy_share = min(0.9, max(0.1, 0.5 + 40 * move + 0.08 * _h(sym, tf, open_t, "b")))
        qv = vol * (o + c) / 2
        return [open_t, f"{o:.8f}", f"{h:.8f}", f"{l:.8f}", f"{c:.8f}", f"{vol:.6f}", open_t + step - 1, f"{qv:.4f}", int(vol * 3) + 1,
                f"{vol * buy_share:.6f}", f"{qv * buy_share:.4f}", "0"]

    # ------------------------------------------------------------ signed futures
    def _equity(self) -> Tuple[float, float, float]:
        upnl = sum((self.price(s) - p["entry"]) * p["qty"] for s, p in self.pos.items())
        used = sum(abs(p["qty"]) * p["entry"] / self.leverage.get(s, 3) for s, p in self.pos.items())
        return self.balance, upnl, max(0.0, self.balance + upnl - used)

    def _signed(self, method: str, path: str, p: Dict[str, str]) -> Tuple[int, Any]:
        if path in ("/fapi/v3/account", "/fapi/v2/account") and method == "GET":
            w, up, av = self._equity()
            return 200, {"totalWalletBalance": f"{w:.8f}", "totalUnrealizedProfit": f"{up:.8f}", "totalMarginBalance": f"{w + up:.8f}", "availableBalance": f"{av:.8f}"}
        if path in ("/fapi/v3/positionRisk", "/fapi/v2/positionRisk") and method == "GET":
            return 200, [{"symbol": s, "positionAmt": f"{q['qty']:.8f}", "entryPrice": f"{q['entry']:.8f}", "markPrice": f"{self.price(s):.8f}",
                          "unRealizedProfit": f"{(self.price(s) - q['entry']) * q['qty']:.8f}", "liquidationPrice": f"{self._liq(s, q):.8f}",
                          "leverage": str(self.leverage.get(s, 3)), "marginType": "isolated"} for s, q in self.pos.items() if q["qty"] != 0]
        if path == "/fapi/v1/openOrders" and method == "GET":
            return 200, [dict(o) for o in self.client_orders.values() if o["symbol"] == p.get("symbol") and o["status"] in ("NEW", "PARTIALLY_FILLED")]
        if path == "/fapi/v1/leverageBracket" and method == "GET":
            syms = [p["symbol"]] if p.get("symbol") else self.symbols
            out = [{"symbol": s, "notionalCoef": 1.0, "brackets": self.brackets_for(s)} for s in syms]
            return 200, out[0] if p.get("symbol") and self.bracket_single_object else out
        if path == "/fapi/v1/positionSide/dual":
            return 200, {"dualSidePosition": self.dual}
        if path == "/fapi/v1/multiAssetsMargin":
            return 200, {"multiAssetsMargin": self.multi_assets}
        if path == "/fapi/v1/marginType" and method == "POST":
            return 200, {"code": 200, "msg": "success"}
        if path == "/fapi/v1/leverage" and method == "POST":
            self.leverage[p["symbol"]] = int(p["leverage"])
            return 200, {"symbol": p["symbol"], "leverage": int(p["leverage"])}
        if path == "/fapi/v1/order":
            if method == "POST":
                return 200, self._new_order(p)
            o = self.client_orders.get(p.get("origClientOrderId", ""))
            if method == "GET":
                if not o:
                    raise _Err(400, -2013, "Order does not exist.")
                self._limit_progress(o)
                return 200, dict(o)
            if method == "DELETE":
                if not o or o["status"] not in ("NEW", "PARTIALLY_FILLED"):
                    raise _Err(400, -2011, "Unknown order sent.")
                o["status"] = "CANCELED"
                return 200, dict(o)
        if path == "/fapi/v1/income" and method == "GET":
            start = int(p.get("startTime", 0))
            return 200, [r for r in self.income if r["symbol"] == p.get("symbol", r["symbol"]) and r["time"] >= start
                         and r["incomeType"] == p.get("incomeType", r["incomeType"])]

        if path == "/fapi/v1/algoOrder":
            if method == "POST":
                return 200, self._new_algo(p)
            if method == "DELETE":
                before = len(self.algos)
                self.algos = [a for a in self.algos if str(a["algoId"]) != p.get("algoId")]
                if len(self.algos) == before:
                    raise _Err(400, -2011, "Unknown order sent.")
                return 200, {"algoId": p["algoId"], "algoStatus": "CANCELED"}
        if path == "/fapi/v1/openAlgoOrders" and method == "GET":
            return 200, [dict(a) for a in self.algos if a["symbol"] == p.get("symbol", a["symbol"])]
        if path == "/fapi/v1/algoOpenOrders" and method == "DELETE":
            self.algos = [a for a in self.algos if a["symbol"] != p["symbol"]]
            return 200, {"code": 200, "msg": "The operation of cancel all open order is done."}
        if path == "/fapi/v1/allOpenOrders" and method == "DELETE":
            return 200, {"code": 200, "msg": "The operation of cancel all open order is done."}
        if path == "/fapi/v1/userTrades" and method == "GET":
            start = int(p.get("startTime", 0))
            return 200, [t for t in self.user_trades if t["symbol"] == p["symbol"] and t["time"] >= start][: int(p.get("limit", 500))]
        raise _Err(404, -1, f"no route {method} {path}")

    def brackets_for(self, symbol: str) -> List[Dict[str, Any]]:
        if symbol in self.bracket_override:
            return self.bracket_override[symbol]
        if symbol in ("BTCUSDT", "ETHUSDT"):
            return [{"bracket": 1, "initialLeverage": 125, "notionalCap": 50_000, "notionalFloor": 0, "maintMarginRatio": 0.004, "cum": 0.0},
                    {"bracket": 2, "initialLeverage": 100, "notionalCap": 600_000, "notionalFloor": 50_000, "maintMarginRatio": 0.005, "cum": 50.0}]
        return [{"bracket": 1, "initialLeverage": 75, "notionalCap": 5_000, "notionalFloor": 0, "maintMarginRatio": 0.01, "cum": 0.0},
                {"bracket": 2, "initialLeverage": 50, "notionalCap": 50_000, "notionalFloor": 5_000, "maintMarginRatio": 0.015, "cum": 25.0}]

    def _liq(self, symbol: str, q: Dict[str, float]) -> float:
        """Isolated liquidation price with the same brackets the exchange publishes (tests can override it)."""
        if symbol in self.liq_override:
            return self.liq_override[symbol]
        from .risk.liquidation import bracket_for, isolated_liquidation_price
        qty, side = abs(q["qty"]), ("LONG" if q["qty"] > 0 else "SHORT")
        br = bracket_for(self.brackets_for(symbol), qty * q["entry"])
        return isolated_liquidation_price(side, q["entry"], qty, self.leverage.get(symbol, 3), br["maintMarginRatio"], br["cum"])

    def _require(self, p: Dict[str, str], *names: str) -> None:
        for n in names:
            if n not in p:
                raise _Err(400, -1102, f"Mandatory parameter '{n}' was not sent, was empty/null, or malformed.")

    def _check_qty(self, sym: str, qty: Decimal, price: float) -> None:
        tick, step, notional = SYMBOL_SPECS.get(sym, ("0.0001", "1", "5"))
        if qty <= 0 or (qty % Decimal(step)) != 0:
            raise _Err(400, -1111, "Precision is over the maximum defined for this asset.")
        if float(qty) * price < float(notional) * 0.999:
            raise _Err(400, -4164, "Order's notional must be no smaller than %s (unless you choose reduce only)." % notional)

    def _new_order(self, p: Dict[str, str]) -> Dict[str, Any]:
        self._require(p, "symbol", "side", "type")
        typ = p["type"]
        if typ in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
            raise _Err(400, -4120, "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.")
        if typ == "LIMIT":
            return self._new_limit(p)
        if typ != "MARKET":
            raise _Err(400, -1116, "Invalid orderType.")
        self._require(p, "quantity")
        sym, side, qty = p["symbol"], p["side"], Decimal(p["quantity"])
        px = self.price(sym)
        reduce_only = p.get("reduceOnly") == "true"
        signed_qty = float(qty) if side == "BUY" else -float(qty)
        cur = self.pos.get(sym, {"qty": 0.0, "entry": 0.0})
        if reduce_only:
            if cur["qty"] == 0 or (cur["qty"] > 0) == (signed_qty > 0):
                raise _Err(400, -2022, "ReduceOnly Order is rejected.")
            signed_qty = max(-cur["qty"], signed_qty) if cur["qty"] > 0 else min(-cur["qty"], signed_qty)
            qty = Decimal(str(abs(signed_qty)))
        else:
            self._check_qty(sym, qty, px)
        coid = p.get("newClientOrderId", f"auto{self._id}")
        if coid in self.client_orders:
            raise _Err(400, -4015, "Client order id is not valid.")
        fill = px * (1.0001 if side == "BUY" else 0.9999)
        realized = self._apply_fill(sym, signed_qty, fill)
        self._id += 1
        order = {"orderId": self._id, "symbol": sym, "status": "FILLED", "clientOrderId": coid, "price": "0", "avgPrice": f"{fill:.8f}",
                 "origQty": str(qty), "executedQty": str(qty), "cumQuote": f"{fill * float(qty):.8f}", "side": side, "type": "MARKET",
                 "reduceOnly": reduce_only, "updateTime": now_ms()}
        self.client_orders[coid] = order
        self._record_trade(sym, side, fill, float(qty), realized)
        return order

    def _new_limit(self, p: Dict[str, str]) -> Dict[str, Any]:
        """Post-only limit entry. `limit_mode`: fill | partial | rest | reject (what the market does with the resting order)."""
        self._require(p, "quantity", "price", "timeInForce")
        sym, side, qty, price = p["symbol"], p["side"], Decimal(p["quantity"]), float(p["price"])
        if p["timeInForce"] != "GTX":
            raise _Err(400, -1116, "fake supports post-only (GTX) limit orders only")
        px = self.price(sym)
        if self.limit_mode == "reject" or (side == "BUY" and price >= px * 1.00005) or (side == "SELL" and price <= px * 0.99995):
            raise _Err(400, -5022, "Due to the order could not be executed as maker, the Post Only order will be rejected.")
        self._check_qty(sym, qty, price)
        coid = p.get("newClientOrderId", f"auto{self._id}")
        if coid in self.client_orders:
            raise _Err(400, -4015, "Client order id is not valid.")
        self._id += 1
        order = {"orderId": self._id, "symbol": sym, "status": "NEW", "clientOrderId": coid, "price": p["price"], "avgPrice": "0",
                 "origQty": str(qty), "executedQty": "0", "cumQuote": "0", "side": side, "type": "LIMIT", "timeInForce": "GTX",
                 "reduceOnly": False, "updateTime": now_ms()}
        self.client_orders[coid] = order
        return dict(order)

    def _limit_progress(self, o: Dict[str, Any]) -> None:
        if o.get("type") != "LIMIT" or o["status"] != "NEW" or self.limit_mode not in ("fill", "partial"):
            return
        step = Decimal(SYMBOL_SPECS.get(o["symbol"], ("0.0001", "1", "5"))[1])
        qty = Decimal(o["origQty"])
        if self.limit_mode == "partial":
            qty = (qty / 2 / step).to_integral_value(rounding="ROUND_FLOOR") * step
        if qty <= 0:
            return
        price = float(o["price"])
        realized = self._apply_fill(o["symbol"], float(qty) if o["side"] == "BUY" else -float(qty), price)
        self._record_trade(o["symbol"], o["side"], price, float(qty), realized)
        o.update(status="FILLED" if self.limit_mode == "fill" else "PARTIALLY_FILLED", executedQty=str(qty), avgPrice=f"{price:.8f}",
                 cumQuote=f"{price * float(qty):.8f}")

    def _apply_fill(self, sym: str, signed_qty: float, price: float) -> float:
        cur = self.pos.get(sym, {"qty": 0.0, "entry": 0.0})
        realized = 0.0
        if cur["qty"] == 0 or (cur["qty"] > 0) == (signed_qty > 0):
            tot = cur["qty"] + signed_qty
            entry = (cur["entry"] * abs(cur["qty"]) + price * abs(signed_qty)) / abs(tot) if tot else 0.0
            self.pos[sym] = {"qty": tot, "entry": entry}
        else:
            closed = min(abs(signed_qty), abs(cur["qty"]))
            realized = (price - cur["entry"]) * closed * (1 if cur["qty"] > 0 else -1)
            rem = cur["qty"] + signed_qty
            self.pos[sym] = {"qty": rem, "entry": cur["entry"] if abs(rem) > 1e-12 else 0.0}
            if abs(rem) < 1e-12:
                self.pos.pop(sym, None)
        self.balance += realized
        return realized

    def _record_trade(self, sym: str, side: str, price: float, qty: float, realized: float) -> None:
        fee = price * qty * 0.0005
        self.balance -= fee
        self.user_trades.append({"symbol": sym, "side": side, "price": f"{price:.8f}", "qty": f"{qty:.8f}", "commission": f"{fee:.8f}",
                                 "commissionAsset": "USDT", "realizedPnl": f"{realized:.8f}", "time": now_ms() - self.fill_lag_ms})

    def _new_algo(self, p: Dict[str, str]) -> Dict[str, Any]:
        self._require(p, "algoType", "symbol", "side", "type", "triggerPrice")
        if p["algoType"] != "CONDITIONAL":
            raise _Err(400, -1116, "Invalid algoType.")
        typ = p["type"]
        if typ not in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
            raise _Err(400, -1116, "Invalid orderType.")
        close_pos = p.get("closePosition") == "true"
        if close_pos and ("quantity" in p or p.get("reduceOnly") == "true"):
            raise _Err(400, -1106, "Parameter 'quantity'/'reduceOnly' sent when not required.")
        if not close_pos:
            self._require(p, "quantity")
        if "clientAlgoId" not in p or not (1 <= len(p["clientAlgoId"]) <= 36):
            raise _Err(400, -4015, "Client algo id is not valid.")
        sym, side, trig = p["symbol"], p["side"], float(p["triggerPrice"])
        px = self.price(sym)
        # SELL stop (long protection) must trigger below the market; BUY stop above. Take-profit is the mirror image.
        stop_like = typ.startswith("STOP")
        must_be_below = (side == "SELL") == stop_like
        if (must_be_below and trig >= px) or ((not must_be_below) and trig <= px):
            raise _Err(400, -2021, "Order would immediately trigger.")
        if close_pos and sym not in self.pos:
            raise _Err(400, -4509, "TIF GTE can only be used with open positions or open orders.")
        if close_pos and any(a["symbol"] == sym and a["side"] == side and a["closePosition"] is True and a["orderType"].startswith("STOP") == stop_like
                             for a in self.algos):
            raise _Err(400, -4130, "An open stop or take profit order with GTE and closePosition in the direction is existing.")
        self._id += 1
        algo = {"algoId": self._id, "clientAlgoId": p["clientAlgoId"], "algoType": "CONDITIONAL", "orderType": typ, "symbol": sym, "side": side,
                "triggerPrice": p["triggerPrice"], "quantity": p.get("quantity", "0"), "closePosition": close_pos,
                "reduceOnly": p.get("reduceOnly") == "true", "workingType": p.get("workingType", "CONTRACT_PRICE"), "algoStatus": "NEW"}
        self.algos.append(algo)
        return {k: algo[k] for k in ("algoId", "clientAlgoId", "algoType", "orderType", "symbol", "side", "triggerPrice", "algoStatus")}

    def _trigger(self, sym: str) -> None:
        px = self.price(sym)
        for a in list(self.algos):
            if a["symbol"] != sym or sym not in self.pos:
                continue
            trig = float(a["triggerPrice"])
            stop_like = a["orderType"].startswith("STOP")
            below = (a["side"] == "SELL") == stop_like
            if (below and px <= trig) or ((not below) and px >= trig):
                pos = self.pos[sym]
                qty = abs(pos["qty"]) if a["closePosition"] else min(float(a["quantity"]), abs(pos["qty"]))
                fill = px
                signed = qty if a["side"] == "BUY" else -qty
                realized = self._apply_fill(sym, signed, fill)
                self._record_trade(sym, a["side"], fill, qty, realized)
                self.algos.remove(a)
                if sym not in self.pos:      # position gone: remaining protective orders are cancelled by the exchange? (no: they linger)
                    pass


class _Err(Exception):
    def __init__(self, status: int, code: int, msg: str):
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg


# =============================================================================== streams
def warm_feed(fake: FakeBinance, trades, books, symbols=None, minutes: int = 12, whale_usdt: float = 150_000.0, bias: float = 0.0) -> None:
    """Fill TradeStore/BookStore as if the WebSocket had been running for `minutes` (no threads, deterministic).

    bias in [-1, 1]: >0 makes aggressive buying (and big buy prints) dominate, <0 selling.
    """
    symbols = list(symbols or fake.symbols)
    now = now_ms()
    for sym in symbols:
        n = minutes * 6
        for i in range(n):
            ts = now - (n - i) * 10_000
            px = fake.price(sym, ts)
            notional = 4_000 + 3_000 * abs(_h(sym, i, "n"))
            buy = _h(sym, i, "s") + bias * 0.9 > 0
            trades.add(sym, ts, px, notional / px, not buy)      # buyer_is_maker=False => aggressive buy
            if i % 14 == 0:
                big_buy = (_h(sym, i, "w") + bias * 1.2) > 0
                trades.add(sym, ts + 1, px, whale_usdt / px, not big_buy)
        px = fake.price(sym)
        trades.add(sym, now - 500, px, 5_000 / px, False)
        books.update(sym, *_depth(px, bias), now)


def _depth(px: float, bias: float = 0.0, levels: int = 20):
    tick = px * 0.00002
    bids = [[f"{px - (i + 1) * tick:.8f}", f"{(1.0 + 0.6 * bias) * 50_000 / px:.6f}"] for i in range(levels)]
    asks = [[f"{px + (i + 1) * tick:.8f}", f"{(1.0 - 0.6 * bias) * 50_000 / px:.6f}"] for i in range(levels)]
    return bids, asks


def mark_streams_live(streams) -> None:
    """Pretend the two websocket connections are up and receiving (for threadless tests)."""
    for conn in (streams.market, streams.public):
        conn.available, conn.connected, conn.last_msg_ms = True, True, now_ms()


class _FakeApp:
    def __init__(self, owner, url, on_open, on_message, on_error, on_close):
        self.owner, self.url = owner, url
        self.on_open, self.on_message, self.on_close = on_open, on_message, on_close
        self.streams: set = set()
        self.closed = threading.Event()

    def send(self, text: str) -> None:
        msg = json.loads(text)
        if msg.get("method") == "SUBSCRIBE":
            self.streams |= set(msg["params"])
        elif msg.get("method") == "UNSUBSCRIBE":
            self.streams -= set(msg["params"])

    def close(self) -> None:
        self.closed.set()

    def run_forever(self, ping_interval=None, ping_timeout=None) -> None:
        self.on_open(self)
        i = 0
        while not self.closed.is_set():
            i += 1
            for st in list(self.streams):
                sym = st.split("@")[0].upper()
                px = self.owner.fake.price(sym)
                if "aggTrade" in st:
                    qty = (4_000 + 3_000 * abs(_h(sym, i, "q"))) / px
                    self.on_message(self, json.dumps({"stream": st, "data": {"e": "aggTrade", "s": sym, "T": now_ms(), "p": f"{px:.8f}",
                                                                           "q": f"{qty:.6f}", "m": _h(sym, i, "m") < 0}}))
                elif "depth" in st:
                    b, a = _depth(px, self.owner.bias)
                    self.on_message(self, json.dumps({"stream": st, "data": {"e": "depthUpdate", "s": sym, "E": now_ms(), "b": b, "a": a}}))
            self.closed.wait(self.owner.interval)
        self.on_close(self, 1000, "closed")


class FakeWSFactory:
    """Drop-in for ws._default_factory that generates synthetic aggTrade/depth messages from the fake price path."""

    def __init__(self, fake: FakeBinance, interval: float = 0.25, bias: float = 0.0):
        self.fake, self.interval, self.bias = fake, interval, bias

    def __call__(self, url, on_open, on_message, on_error, on_close):
        return _FakeApp(self, url, on_open, on_message, on_error, on_close)


class _IdleApp:
    """WebSocket stand-in that connects to nothing and just waits (threadless tests set the health flags themselves)."""

    def __init__(self, *_a, **_k):
        self.closed = threading.Event()

    def send(self, _text: str) -> None:
        return None

    def close(self) -> None:
        self.closed.set()

    def run_forever(self, ping_interval=None, ping_timeout=None) -> None:
        self.closed.wait()


def idle_ws_factory(*_a, **_k):
    return _IdleApp()


# =============================================================================== harness
def make_engine(fake: Optional[FakeBinance] = None, env: Optional[Dict[str, str]] = None, *, live_ws: bool = False, warm: bool = True,
                bias: float = 0.0, mailer=None, db=None, start: bool = True, storage: str = ":memory:", telegram_transport=None,
                live_gate: bool = True):
    """Engine wired to a FakeBinance. Default: LIVE mode with confirmation + keys (the strictest path), no network, no threads."""
    from .config import LIVE_CONFIRM_PHRASE, load_settings
    from .db import Database
    from .exchange.rest import RestClient
    from .runtime.engine import Engine

    fake = fake or FakeBinance()
    base = {"MODE": "live", "LIVE_TRADING_CONFIRM": LIVE_CONFIRM_PHRASE, "BINANCE_API_KEY": fake.api_key, "BINANCE_API_SECRET": fake.api_secret,
            "IMPERION_STORAGE_DIR": "storage-test", "ADMIN_TOKEN": "admin-token-0123456789", "TRADINGVIEW_WEBHOOK_SECRET": "tv-secret-0123456789",
            "RADAR_COUNT": "3", "TESTNET_EVIDENCE_SECRET": TEST_EVIDENCE_SECRET}
    cfg = load_settings({**base, **(env or {})})
    rest = RestClient(cfg.rest_base, transport=fake.transport)
    trade = RestClient(cfg.trade_base, cfg.api_key, cfg.api_secret, transport=fake.transport, guard=rest.guard)
    spot = RestClient(cfg.spot_base, cfg.api_key, cfg.api_secret, transport=fake.transport, guard=rest.guard)
    factory = FakeWSFactory(fake) if live_ws else idle_ws_factory
    engine = Engine(cfg, db=db or Database(storage), rest=rest, trade_rest=trade, spot_rest=spot, ws_factory=factory,
                    mailer=mailer or (lambda *a, **k: (True, "sent")), telegram_transport=telegram_transport)
    _ENGINES.add(engine)
    if live_gate and cfg.mode == "live" and engine.db.kv_get("gate.live_approval") is None:
        seed_live_gate(engine.db, cfg.testnet_evidence_secret)   # the harness stands in for a passed testnet run + operator approval
    if start:
        engine.start(http=False, threads=False)
        if warm:
            warm_feed(fake, engine.trades, engine.books, minutes=12, bias=bias)
        if not live_ws:
            mark_streams_live(engine.streams)
    return engine, fake
