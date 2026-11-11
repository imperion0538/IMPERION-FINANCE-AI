"""Binance REST client: HMAC signing, clock sync, rate-limit discipline.

Facts this module is built on (Binance USDⓈ-M Futures docs, checked Nov 2026):
  * signature = HMAC-SHA256(secret, full query string), appended last as `signature`
  * header X-MBX-APIKEY; `timestamp` + `recvWindow` required on signed calls
  * 429 = back off; 418 = IP ban (2 min .. 3 days) -> stop ALL traffic until it ends
  * used weight is returned in X-MBX-USED-WEIGHT-1M
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from ..config import VERSION

log = logging.getLogger("imperion.rest")

Transport = Callable[[str, str, Dict[str, str], float], Tuple[int, Dict[str, str], str]]


class BinanceAPIError(Exception):
    def __init__(self, code: int, msg: str, status: int = 0):
        super().__init__(f"Binance error {code}: {msg} (HTTP {status})")
        self.code, self.msg, self.status = code, msg, status


class RateLimited(BinanceAPIError):
    def __init__(self, msg: str, status: int = 429, retry_after: float = 0.0):
        super().__init__(-1003, msg, status)
        self.retry_after = retry_after


class NetworkError(Exception):
    """Connection/timeout problem; the outcome of a POST is unknown."""


def _default_transport(method: str, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, Dict[str, str], str]:
    import requests  # local import keeps the module importable without requests in unit tests

    try:
        resp = requests.request(method, url, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        raise NetworkError("timeout") from exc
    except requests.RequestException as exc:
        raise NetworkError(type(exc).__name__) from exc
    return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.text


class RateGuard:
    """Shared across all clients of the process (same IP)."""

    SOFT_WEIGHT_LIMIT = 1800  # of 2400/min

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.banned_until = 0.0
        self.cooldown_until = 0.0
        self.used_weight = 0
        self.weight_ts = 0.0
        self.max_weight = 0
        self.requests = 0
        self.count_429 = 0
        self.count_418 = 0
        self.last_limit_ts = 0.0

    def note_headers(self, headers: Dict[str, str]) -> None:
        w = headers.get("x-mbx-used-weight-1m")
        if w and w.isdigit():
            with self._lock:
                self.used_weight, self.weight_ts = int(w), time.time()
                self.max_weight = max(self.max_weight, int(w))

    def note_429(self, retry_after: float) -> None:
        with self._lock:
            self.cooldown_until = max(self.cooldown_until, time.time() + max(retry_after, 5.0))
            self.count_429 += 1
            self.last_limit_ts = time.time()

    def note_418(self, retry_after: float) -> None:
        with self._lock:
            self.banned_until = max(self.banned_until, time.time() + max(retry_after, 120.0))
            self.count_418 += 1
            self.last_limit_ts = time.time()

    def stats(self) -> Dict[str, Any]:
        """Rate-limit telemetry for /status, the dashboard and the monitoring alert."""
        now = time.time()
        with self._lock:
            fresh = now - self.weight_ts < 60
            return {"requests": self.requests, "used_weight": self.used_weight if fresh else 0, "max_weight": self.max_weight,
                    "soft_limit": self.SOFT_WEIGHT_LIMIT, "count_429": self.count_429, "count_418": self.count_418,
                    "cooldown_s": round(max(0.0, self.cooldown_until - now), 1), "banned_s": round(max(0.0, self.banned_until - now), 1)}

    def check(self, priority: bool) -> None:
        now = time.time()
        if now < self.banned_until:
            raise RateLimited(f"IP banned for another {self.banned_until - now:.0f}s", 418, self.banned_until - now)
        if not priority:
            if now < self.cooldown_until:
                raise RateLimited(f"rate-limit cooldown {self.cooldown_until - now:.0f}s", 429, self.cooldown_until - now)
            if self.used_weight >= self.SOFT_WEIGHT_LIMIT and now - self.weight_ts < 60:
                raise RateLimited(f"local weight throttle (used {self.used_weight}/min)", 429, 60 - (now - self.weight_ts))


class RestClient:
    def __init__(
        self,
        base: str,
        api_key: str = "",
        api_secret: str = "",
        *,
        transport: Optional[Transport] = None,
        guard: Optional[RateGuard] = None,
        recv_window: int = 5000,
        timeout: float = 10.0,
        name: str = "rest",
    ):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self._secret = api_secret.encode() if api_secret else b""
        self.transport = transport or _default_transport
        self.guard = guard or RateGuard()
        self.recv_window = recv_window
        self.timeout = timeout
        self.name = name
        self.time_offset_ms = 0

    # ---- low level -----------------------------------------------------
    def _sign(self, query: str) -> str:
        return hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()

    def _build(self, params: Optional[Dict[str, Any]], signed: bool) -> str:
        items: List[Tuple[str, str]] = []
        for k, v in (params or {}).items():
            if v is None:
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            items.append((k, str(v)))
        if signed:
            items.append(("timestamp", str(int(time.time() * 1000) + self.time_offset_ms)))
            items.append(("recvWindow", str(self.recv_window)))
        query = urlencode(items)
        if signed:
            query += "&signature=" + self._sign(query)
        return query

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        signed: bool = False,
        priority: bool = False,
        retries: int = 2,
    ) -> Any:
        if signed and not (self.api_key and self._secret):
            raise BinanceAPIError(-2014, "API key/secret not configured", 0)
        method = method.upper()
        attempt = 0
        while True:
            attempt += 1
            self.guard.check(priority)
            self.guard.requests += 1
            query = self._build(params, signed)
            url = f"{self.base}{path}" + (f"?{query}" if query else "")
            headers = {"Accept": "application/json", "User-Agent": f"imperion-finance/{VERSION}"}
            if self.api_key:
                headers["X-MBX-APIKEY"] = self.api_key
            try:
                status, rh, text = self.transport(method, url, headers, self.timeout)
            except NetworkError:
                # GET is safe to retry; a POST/DELETE outcome is unknown -> caller must reconcile.
                if method == "GET" and attempt <= retries:
                    time.sleep(0.4 * attempt)
                    continue
                raise
            self.guard.note_headers(rh)
            retry_after = float(rh.get("retry-after", "0") or 0)
            if status == 418:
                self.guard.note_418(retry_after)
                raise RateLimited("HTTP 418 IP banned (stop all requests)", 418, retry_after)
            if status == 429:
                self.guard.note_429(retry_after)
                raise RateLimited("HTTP 429 rate limit", 429, retry_after)
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                raise BinanceAPIError(-1, f"invalid JSON (HTTP {status}): {text[:120]}", status) from None
            if status == 451:
                raise BinanceAPIError(-451, "HTTP 451: this server region is restricted by Binance", status)
            if status >= 400 or (isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0):
                code = int(data.get("code", -status)) if isinstance(data, dict) else -status
                msg = str(data.get("msg", text[:120])) if isinstance(data, dict) else text[:120]
                if code == -1021 and attempt <= 1 and signed:  # timestamp outside recvWindow
                    try:
                        self.sync_time()
                    except Exception:  # pragma: no cover - best effort
                        pass
                    continue
                raise BinanceAPIError(code, msg, status)
            return data

    def sync_time(self) -> int:
        t0 = time.time() * 1000
        data = self.request("GET", "/fapi/v1/time", priority=True)
        t1 = time.time() * 1000
        server = int(data["serverTime"])
        self.time_offset_ms = int(server - (t0 + t1) / 2)
        return self.time_offset_ms

    # ---- convenience wrappers (public) --------------------------------
    def klines(self, symbol: str, interval: str, limit: int = 500, start_time: Optional[int] = None,
               end_time: Optional[int] = None) -> List[list]:
        params: Dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        data = self.request("GET", "/fapi/v1/klines", params)
        if not isinstance(data, list):
            raise BinanceAPIError(-1, "unexpected klines payload", 200)
        return data

    def exchange_info(self) -> Dict[str, Any]:
        return self.request("GET", "/fapi/v1/exchangeInfo")

    def ticker_24h(self) -> List[Dict[str, Any]]:
        return self.request("GET", "/fapi/v1/ticker/24hr")

    def premium_index(self) -> List[Dict[str, Any]]:
        data = self.request("GET", "/fapi/v1/premiumIndex")
        return data if isinstance(data, list) else [data]

    def price(self, symbol: str) -> float:
        data = self.request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def book_ticker(self, symbol: str) -> Dict[str, Any]:
        return self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
