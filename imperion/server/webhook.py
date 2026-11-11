"""TradingView alert intake and the worker that actually evaluates queued alerts.

TradingView is only a HINT: an alert makes the engine evaluate that symbol immediately and the external agent reports
the alert direction. The Director still needs a complete playbook, team consensus, data health and every risk gate.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from ..config import STYLES, Settings
from ..db import Database
from ..util import now_ms

log = logging.getLogger("imperion.webhook")

LONG_WORDS = {"BUY", "LONG", "BULLISH", "ENTER_LONG"}
SHORT_WORDS = {"SELL", "SHORT", "BEARISH", "ENTER_SHORT"}
IGNORED_WORDS = {"CLOSE", "EXIT", "FLAT", "CLOSE_LONG", "CLOSE_SHORT"}
MAX_ALERT_AGE_MS = 10 * 60_000
_TF_ALIASES = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h", "240": "4h", "D": "1d", "1D": "1d", "W": "1w", "1W": "1w",
               "1M": "1m", "5M": "5m", "15M": "15m", "30M": "30m", "1H": "1h", "4H": "4h"}


def normalize_symbol(raw: str) -> str:
    s = str(raw or "").upper().strip()
    s = s.split(":")[-1]                      # BINANCE:BTCUSDT.P
    s = re.sub(r"\.P$|PERP$|\.PERP$", "", s)
    return s


def normalize_timeframe(raw: Any) -> Optional[str]:
    if raw in (None, ""):
        return None
    t = str(raw).strip()
    if t.lower() in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"):
        return t.lower()
    return _TF_ALIASES.get(t.upper())


def parse_body(body: bytes, content_type: str = "") -> Dict[str, Any]:
    """TradingView sends JSON or plain text. Plain text format: 'BTCUSDT long 15'."""
    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    parts = text.replace(",", " ").split()
    if len(parts) >= 2:
        out = {"symbol": parts[0], "action": parts[1]}
        if len(parts) >= 3:
            out["timeframe"] = parts[2]
        return out
    raise ValueError("unparseable alert body")


def normalize_alert(data: Dict[str, Any], allowed_symbols) -> Dict[str, Any]:
    symbol = normalize_symbol(data.get("symbol") or data.get("ticker") or "")
    if symbol not in set(allowed_symbols):
        raise ValueError(f"symbol {symbol or '?'} is not in the traded universe")
    action = str(data.get("action") or data.get("side") or data.get("direction") or "").upper().replace(" ", "_")
    if action in LONG_WORDS:
        side = "LONG"
    elif action in SHORT_WORDS:
        side = "SHORT"
    elif action in IGNORED_WORDS:
        side = "CLOSE"
    else:
        raise ValueError("action must be buy/long or sell/short (or close)")
    out = {"symbol": symbol, "side": side, "timeframe": normalize_timeframe(data.get("timeframe") or data.get("interval")),
           "alert_id": str(data.get("alert_id"))[:128] if data.get("alert_id") else None, "strategy": str(data.get("strategy", ""))[:100],
           "received_ms": now_ms()}
    price = data.get("price")
    try:
        out["price"] = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        out["price"] = None
    return out


def idempotency_key(alert: Dict[str, Any]) -> str:
    if alert.get("alert_id"):
        return "tv:" + alert["alert_id"]
    core = f"{alert['symbol']}|{alert['side']}|{alert.get('timeframe')}|{alert['received_ms'] // 60_000}"
    return "tv:" + hashlib.sha256(core.encode()).hexdigest()[:32]


def styles_for(alert: Dict[str, Any], active: List[str]) -> List[str]:
    tf = alert.get("timeframe")
    if not tf:
        return list(active)
    hit = [s for s in active if tf in (STYLES[s].trigger_tf, STYLES[s].setup_tf)]      # styles that use this timeframe as setup/trigger
    return hit or list(active)


class WebhookWorker(threading.Thread):
    def __init__(self, cfg: Settings, db: Database, evaluate: Callable[..., Dict[str, Any]]):
        super().__init__(name="webhook-worker", daemon=True)
        self.cfg, self.db, self.evaluate = cfg, db, evaluate
        self._stop_evt = threading.Event()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        if self.is_alive() and self is not threading.current_thread():
            self.join(timeout)

    def process_pending(self) -> int:
        n = 0
        for row in self.db.pending_webhooks():
            alert = json.loads(row["payload_json"])
            if alert["side"] == "CLOSE":
                self.db.finish_webhook(row["id"], "IGNORED", {"reason": "exits are managed by the system"})
                n += 1
                continue
            if now_ms() - alert["received_ms"] > MAX_ALERT_AGE_MS:
                self.db.finish_webhook(row["id"], "STALE", {"reason": "alert older than 10 minutes"})
                n += 1
                continue
            results = []
            for style in styles_for(alert, list(self.cfg.active_styles)):
                try:
                    res = self.evaluate(alert["symbol"], style, hint=alert["side"], source="tradingview")
                    results.append({"style": style, "direction": res.get("direction"), "approved": res.get("approved"),
                                    "status": res.get("status"), "reasons": (res.get("reasons") or [])[:4]})
                    if res.get("traded"):
                        break
                except Exception as exc:
                    results.append({"style": style, "error": f"{type(exc).__name__}: {exc}"})
            self.db.finish_webhook(row["id"], "PROCESSED", {"alert": alert, "results": results})
            self.db.event("INFO", "tradingview", f"alert {alert['symbol']} {alert['side']} -> " + "; ".join(f"{r['style']}:{r.get('direction') or r.get('error')}" for r in results))
            n += 1
        return n

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.process_pending()
            except Exception:
                log.exception("webhook worker error")
            self._stop_evt.wait(1.0)
