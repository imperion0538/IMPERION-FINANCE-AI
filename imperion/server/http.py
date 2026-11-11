"""Tiny stdlib HTTP server: /health, TradingView webhook, and token-protected admin endpoints."""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from ..config import Settings
from ..db import Database
from .dashboard import DASHBOARD_HTML
from .webhook import idempotency_key, normalize_alert, parse_body

log = logging.getLogger("imperion.http")

MAX_BODY = 32 * 1024
MIN_SECRET_LEN = 16


class _AuthLimiter:
    """Global failed-auth throttle (cheap brute-force protection)."""

    def __init__(self, limit: int = 20, window_s: int = 300):
        self.limit, self.window = limit, window_s
        self._fails: list = []
        self._lock = threading.Lock()

    def blocked(self) -> bool:
        now = time.time()
        with self._lock:
            self._fails = [t for t in self._fails if now - t < self.window]
            return len(self._fails) >= self.limit

    def fail(self) -> None:
        with self._lock:
            self._fails.append(time.time())


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def make_server(cfg: Settings, db: Database, api, host: str = "0.0.0.0", port: Optional[int] = None) -> ThreadingHTTPServer:
    """`api` exposes: health(), status(), halt(reason), resume(), flatten(), send_report(), ai_summary(), ai_run(),
    ai_approve(version), ai_rollback()."""
    limiter = _AuthLimiter()

    class Handler(BaseHTTPRequestHandler):
        server_version = "imperion"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet
            log.debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers
        def _send(self, code: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("invalid Content-Length") from None
            if length < 0 or length > MAX_BODY:
                raise ValueError("body too large")
            return self.rfile.read(length) if length > 0 else b""

        def _bearer(self) -> str:
            h = self.headers.get("Authorization", "")
            return h[7:].strip() if h.lower().startswith("bearer ") else self.headers.get("X-Webhook-Secret", "").strip()

        def _admin_ok(self) -> bool:
            if len(cfg.admin_token) < MIN_SECRET_LEN:
                self._send(503, {"error": "admin endpoints disabled: set ADMIN_TOKEN (>=16 chars)"})
                return False
            if limiter.blocked():
                self._send(429, {"error": "too many failed attempts"})
                return False
            if not _eq(self._bearer(), cfg.admin_token):
                limiter.fail()
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        # -- routes
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/health"):
                health = api.health()
                return self._send(200 if health.get("ready") else 503, health)
            if path == "/dashboard":              # static page without data or secrets; it calls /status with the token
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
                self.end_headers()
                self.wfile.write(body)
                return None
            if path == "/status":
                if not self._admin_ok():
                    return
                return self._send(200, api.status())
            if path == "/ai":
                if not self._admin_ok():
                    return
                return self._send(200, api.ai_summary())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            try:
                body = self._body()
            except ValueError as exc:
                return self._send(400 if "invalid" in str(exc) else 413, {"error": str(exc)})
            if path == "/webhook/tradingview":
                return self._webhook(body)
            if not path.startswith("/admin/"):
                return self._send(404, {"error": "not found"})
            if not self._admin_ok():
                return
            try:
                if path == "/admin/halt":
                    reason = (qs.get("reason", ["manual halt via admin API"])[0])[:200]
                    api.halt(reason)
                    return self._send(200, {"halted": True, "reason": reason})
                if path == "/admin/resume":
                    api.resume()
                    return self._send(200, {"halted": False})
                if path == "/admin/flatten":
                    return self._send(200, {"flattened": api.flatten()})
                if path == "/admin/report":
                    ok, detail = api.send_report()
                    return self._send(200 if ok else 502, {"sent": ok, "detail": detail})
                if path == "/admin/ai/run":
                    return self._send(200, api.ai_run())
                if path == "/admin/ai/approve":
                    ver = int(qs.get("version", ["0"])[0])
                    return self._send(200, {"approved": api.ai_approve(ver), "version": ver})
                if path == "/admin/ai/rollback":
                    return self._send(200, {"rolled_back_to": api.ai_rollback()})
            except Exception as exc:
                log.exception("admin handler failed")
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            self._send(404, {"error": "not found"})

        def _webhook(self, body: bytes) -> None:
            secret = cfg.webhook_secret
            if len(secret) < MIN_SECRET_LEN:
                return self._send(503, {"error": "webhook disabled: set TRADINGVIEW_WEBHOOK_SECRET (>=16 chars)"})
            if limiter.blocked():
                return self._send(429, {"error": "too many failed attempts"})
            try:
                data = parse_body(body, self.headers.get("Content-Type", ""))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            supplied = self._bearer() or str(data.pop("secret", "") or "")
            data.pop("secret", None)
            if not _eq(supplied, secret):
                limiter.fail()
                return self._send(401, {"error": "unauthorized"})
            try:
                alert = normalize_alert(data, cfg.symbols)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            res = db.enqueue_webhook(idempotency_key(alert), alert)
            self._send(200, {"accepted": True, "id": res["id"], "duplicate": res["duplicate"], "status": "QUEUED_FOR_EVALUATION"})

    server = ThreadingHTTPServer((host, cfg.port if port is None else port), Handler)
    server.daemon_threads = True
    return server
