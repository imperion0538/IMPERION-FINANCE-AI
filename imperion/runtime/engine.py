"""Runtime engine: wires market data, the Director and its agents, risk, the AI layer, execution, notifications and
the HTTP server, and runs them in supervised threads.

Threads
  candle-poller  : REST candles when a candle closes (execution universe on 8 timeframes, radar on 1h)
  decision       : evaluates every (symbol, style) once per new trigger-timeframe candle
  manage         : guardian (stops / TP / trailing / reconciliation), equity + auto-halts, dry-mode price feed
  maintenance    : derivatives, radar, shadow resolution, learning cycle, daily report, e-mail alerts, pruning
  telegram       : commands + outbound queue
  webhook-worker : TradingView alerts -> evaluate()
  http           : /health, /dashboard, admin, webhook
Without exchange credentials (or without the live confirmation) the engine runs OBSERVE-ONLY: full analysis,
signals, shadow learning and reports, but it can never send an order.
"""
from __future__ import annotations

import json
import math
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..analysis.market import breadth, session
from ..analysis.mtf import cached_tf_bias, mtf_view
from ..analysis.radar import DEEP_STYLE, DEEP_TFS, select_deep, tier1
from ..analysis.orderflow import live_flow
from ..config import CORE_NAME, CORE_VERSION, STYLES, TIMEFRAMES, VERSION, Settings
from ..core.director import Director
from ..core.planner import round_trip_cost_pct
from ..data.admission import FILTER_MAX_AGE_MS, RadarAdmission, fresh
from ..data.candles import CandlePoller, CandleStore
from ..data.health import HealthMonitor, data_quality_gate
from ..data.microstructure import BookStore, TradeStore
from ..data.universe import DerivativesStore, Radar
from ..db import Database
from ..exchange.filters import SymbolFilters, parse_exchange_info
from ..exchange.rest import RateGuard, RestClient
from ..exchange.user_stream import UserStream
from ..exchange.ws import MarketStreams
from ..execution.binance import BinanceExecutor
from ..execution.lifecycle import Lifecycle
from ..execution.sim import SimExecutor
from ..gate import live_gate_status
from ..learning.service import LearningService
from ..learning.shadow import ShadowTracker
from ..models import Analysis
from ..notify import telegram as tgm
from ..reports import daily
from ..risk.manager import RiskManager
from ..risk.inventory import InventoryManager
from ..analysis.toxicity import toxicity_score, toxicity_veto
from ..server.http import make_server
from ..server.webhook import WebhookWorker
from ..util import iso, last_closed_open, now_ms, utc_day
from .telegram_service import TelegramService

log = logging.getLogger("imperion.engine")

NO_TRADE_LOG_EVERY_MS = 15 * 60_000        # NO TRADE decisions without a candidate plan are stored at most this often
MAIL_MIN_INTERVAL_S = 120
MAIL_MAX_PER_DAY = 40
RADAR_TF = "1h"


class Engine:
    def __init__(self, cfg: Settings, *, db: Optional[Database] = None, rest: Optional[RestClient] = None,
                 trade_rest: Optional[RestClient] = None, spot_rest: Optional[RestClient] = None,
                 ws_factory: Optional[Callable] = None, mailer: Optional[Callable] = None, executor: Any = None,
                 filters: Optional[Dict[str, SymbolFilters]] = None, telegram_transport: Optional[Callable] = None):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.guard = rest.guard if rest is not None else RateGuard()      # one guard: every client shares the IP budget
        self.rest = rest or RestClient(cfg.rest_base, guard=self.guard, name="public")
        self.filters: Dict[str, SymbolFilters] = filters if filters is not None else {}
        self.mailer = mailer or daily.send_mail
        self.admission = RadarAdmission(cfg)
        self._filters_updated_ms = now_ms() if filters else 0
        self._current_filter_symbols = set(self.filters)

        # market data
        self.candles, self.trades, self.books = CandleStore(), TradeStore(cfg.whale_floor_usdt), BookStore()
        self.deriv, self.radar = DerivativesStore(), Radar()
        self._ws_factory = ws_factory
        self.streams = MarketStreams(cfg.ws_base, self.trades, self.books, ws_factory)
        self.timeframes = list(TIMEFRAMES)
        self.poller = CandlePoller(self.rest, self.candles, cfg.symbols, self.timeframes)
        self.radar_poller = CandlePoller(self.rest, self.candles, (), (RADAR_TF,))
        self.deep_poller = CandlePoller(self.rest, self.candles, (), DEEP_TFS)
        self.radar_ranked: List[Dict[str, Any]] = []
        self.radar_deep: Dict[str, Dict[str, Any]] = {}
        self.health_mon = HealthMonitor(self.candles, self.trades, self.books, self.streams, cfg.max_spread_pct, cfg.require_streams)

        # intelligence
        self.risk = RiskManager(cfg, self.db)
        self.inventory = InventoryManager(cfg.max_concurrent_positions)
        self.shadows = ShadowTracker(self.db, fetch=lambda sym, tf, start, limit: self.rest.klines(sym, tf, limit=limit, start_time=start))
        self.learning = LearningService(cfg, self.db, self.shadows)
        self.director = Director(cfg, self.candles, self.trades, self.books, self.deriv, weights_provider=self.learning.learner.active)

        # execution (None => observe-only)
        self.trade_rest, self.spot_rest = trade_rest, spot_rest
        self.exec: Any = executor
        self.lifecycle: Optional[Lifecycle] = None
        self.preflight_result: Optional[Dict[str, Any]] = None
        self._preflight_ok = False
        self.observe_reason = ""
        self.user_stream: Optional[UserStream] = None

        # notifications
        self.tg: Optional[tgm.Notifier] = tgm.Notifier(cfg.telegram_token, cfg.telegram_chat_id, telegram_transport) if cfg.telegram_enabled else None
        self.telegram = TelegramService(self)

        # state
        self._started_ms = now_ms()
        self._ready = False
        self._stop = threading.Event()
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._manage_wake = threading.Event()
        self._threads: List[threading.Thread] = []
        self._trade_lock = threading.RLock()
        self._last_eval: Dict[Tuple[str, str], int] = {}
        self._last_stored: Dict[Tuple[str, str], int] = {}
        self._last_result: Dict[str, Dict[str, Any]] = {}
        self.last_analysis: Dict[Tuple[str, str], Analysis] = {}
        self._acct_cache: Tuple[float, Any] = (0.0, None)
        self._err_last: Dict[str, float] = {}
        self._timers: Dict[str, float] = {}
        self._mail_sent: Deque[float] = deque()
        self._mail_lock = threading.Lock()
        self._http = None
        self._webhook_worker: Optional[WebhookWorker] = None
        self.last_decision_ms = 0
        self._rate_seen: Dict[str, int] = {}

    # ================================================================== setup
    def _build_executor(self) -> None:
        cfg = self.cfg
        if self.exec is not None:
            self.observe_reason = ""
            return
        if cfg.mode == "dry":
            self.exec = SimExecutor(cfg, self.filters, lambda s: self.trades.last_price(s) or self.candles.last_price(s))
            return
        if not cfg.orders_possible:
            self.observe_reason = cfg.describe_mode()
            return
        self.trade_rest = self.trade_rest or RestClient(cfg.trade_base, cfg.api_key, cfg.api_secret, guard=self.guard, name="trade")
        self.spot_rest = self.spot_rest or RestClient(cfg.spot_base, cfg.api_key, cfg.api_secret, guard=self.guard, name="spot")
        self.exec = BinanceExecutor(cfg, self.trade_rest, self.spot_rest, self.filters)

    def _load_filters(self) -> bool:
        try:
            current = parse_exchange_info(self.rest.exchange_info())
            self.filters.update(current)
            self._current_filter_symbols = set(current)
            self._filters_updated_ms = now_ms()
            missing = [s for s in self.cfg.symbols if s not in self.filters]
            if missing:
                self.db.event("ERROR", "filters_missing", f"exchangeInfo has no entry for {missing}")
            return not missing
        except Exception as exc:
            self._filters_updated_ms = 0
            self.db.event("ERROR", "filters_failed", f"exchangeInfo failed: {exc}")
            return False

    def run_preflight(self) -> Dict[str, Any]:
        if isinstance(self.exec, BinanceExecutor):
            res = self.exec.preflight()
        else:
            res = {"ok": self.exec is not None, "checks": {"executor": getattr(self.exec, "mode", None)}, "errors": [], "warnings": []}
        self.preflight_result = res
        self._preflight_ok = bool(res["ok"])
        self.db.event("INFO" if res["ok"] else "CRITICAL", "preflight", "preflight " + ("passed" if res["ok"] else "FAILED: " + "; ".join(res["errors"])), res)
        for w in res.get("warnings", []):
            self.db.event("WARNING", "preflight_warning", w)
        return res

    def start(self, *, http: bool = True, threads: bool = True, bootstrap: bool = True) -> None:
        cfg = self.cfg
        log.info("IMPERION FINANCE v%s | %s %s | mode: %s | symbols: %d | styles: %s",
                 VERSION, CORE_NAME, CORE_VERSION, cfg.describe_mode(), len(cfg.symbols), ",".join(cfg.active_styles))
        if http:
            self._http = make_server(cfg, self.db, self)
            t = threading.Thread(target=self._http.serve_forever, name="http", daemon=True)
            t.start()
            self._threads.append(t)
        n_trades = (self.db.one("SELECT COUNT(*) AS c FROM trades") or {"c": 0})["c"]
        self.db.event("INFO", "storage", f"database {self.db.path}: {n_trades} trades on record"
                      + ("" if n_trades else " (fresh database: if you redeployed, mount a persistent volume!)"))
        if not self.filters and not self._stop.is_set():
            self._load_filters()
        self._build_executor()
        if bootstrap and not self._stop.is_set():
            n = self.poller.bootstrap(stop=self._stop)
            log.info("candle bootstrap: %d/%d series", n, len(cfg.symbols) * len(self.timeframes))
            if not self._stop.is_set():
                self._safe(lambda: self.deriv.refresh(self.rest), "derivatives")
                self._safe(self._refresh_radar, "radar")
        if self._stop.is_set():                         # SIGTERM during startup: stop() takes it from here, no new threads
            log.warning("shutdown requested during startup; skipping the remaining start-up steps")
            return
        self.streams.start(self._stream_symbols())
        if not self.streams.available:
            self.db.event("WARNING", "no_websocket", "websocket-client is not installed: live order-flow is unavailable and trading is blocked while REQUIRE_STREAMS is on")
        if self.exec is not None:
            self.lifecycle = Lifecycle(cfg, self.db, self.risk, self.exec, alert=self.alert)
            self.run_preflight()
            if cfg.mode == "live":
                gate = self.live_gate()
                self.db.event("WARNING" if gate["ok"] else "CRITICAL", "live_gate",
                              "live gate OPEN (testnet evidence + manual approval)" if gate["ok"] else "LIVE GATE CLOSED: no order will be sent: " + "; ".join(gate["reasons"]))
            self._safe(lambda: self.lifecycle.manage(), "startup reconcile")
            self._sync_execution_feeds()
            if cfg.user_stream and isinstance(self.exec, BinanceExecutor) and threads:
                self.user_stream = UserStream(self.trade_rest, cfg.ws_base, cfg.user_stream_path, lambda _e: self._manage_wake.set(), self._ws_factory)
                if not self.user_stream.start():
                    self.db.event("WARNING", "user_stream", f"user data stream unavailable ({self.user_stream.last_error}); guardian keeps polling REST")
        else:
            self.db.event("WARNING", "observe_only", f"OBSERVE-ONLY: {self.observe_reason}. No order can be sent.")
        self._webhook_worker = WebhookWorker(cfg, self.db, self.evaluate)
        self._webhook_worker.start()
        self._ready = True
        if threads:
            loops = [("candle-poller", self._loop_candles), ("decision", self._loop_decision), ("manage", self._loop_manage),
                     ("maintenance", self._loop_maintenance)]
            if self.tg is not None:
                loops.append(("telegram", self.telegram.loop))
            for name, fn in loops:
                t = threading.Thread(target=self._guarded(fn, name), name=name, daemon=True)
                t.start()
                self._threads.append(t)
        self.telegram.on_start()
        self.db.event("INFO", "start", f"engine started v{VERSION} ({CORE_NAME} {CORE_VERSION}) mode={cfg.describe_mode()}")

    def run_forever(self) -> None:
        try:
            while not self._stop.wait(1.0):
                pass
        finally:
            self.stop()

    def stop(self, timeout: float = 10.0) -> List[str]:
        """Stop every thread this engine started and wait for them. Idempotent.

        Returns the names of threads that did not finish within `timeout` (normally an empty list). Open positions are
        not touched: they keep their exchange-side stops.
        """
        with self._stop_lock:
            first = not self._stopped
            self._stopped = True
        self._stop.set()
        self._manage_wake.set()
        if self._webhook_worker:
            self._webhook_worker.stop()
        for closer in (self.streams.stop, (self.user_stream.stop if self.user_stream else None)):
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
        if self._http:
            try:
                self._http.shutdown()
                self._http.server_close()
            except Exception:
                pass
            self._http = None
        deadline = time.time() + timeout
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(max(0.0, deadline - time.time()))
        stuck = [t.name for t in self._threads if t.is_alive()]
        if first:
            if stuck:
                log.error("threads still running after stop(): %s", stuck)
            self.db.event("WARNING" if stuck else "INFO", "stop",
                          "engine stopped (open positions keep their exchange-side stops)" + (f"; still running: {stuck}" if stuck else ""))
        return stuck

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ================================================================== helpers
    def _safe(self, fn: Callable[[], Any], what: str) -> Any:
        try:
            return fn()
        except Exception as exc:
            self._throttled_error(what, f"{what} failed: {type(exc).__name__}: {exc}")
            return None

    def _throttled_error(self, key: str, message: str, every_s: float = 300.0) -> None:
        if time.time() - self._err_last.get(key, 0.0) >= every_s:
            self._err_last[key] = time.time()
            self.db.event("ERROR", "runtime_error", message)
        log.warning(message)

    def _guarded(self, fn: Callable[[], None], name: str) -> Callable[[], None]:
        def run() -> None:
            while not self._stop.is_set():
                try:
                    fn()
                    return
                except Exception:
                    log.exception("thread %s crashed; restarting in 5s", name)
                    self._throttled_error(f"thread-{name}", f"thread {name} crashed (restarted)")
                    self._stop.wait(5.0)
        return run

    def _due(self, name: str, every_s: float) -> bool:
        if time.time() - self._timers.get(name, 0) >= every_s:
            self._timers[name] = time.time()
            return True
        return False

    def _radar_symbol_allowed(self, symbol: str, now: int) -> bool:
        if not self.cfg.radar_trading_enabled or (self.cfg.radar_trade_allowlist and symbol not in self.cfg.radar_trade_allowlist):
            return False
        if symbol not in self.admission.symbols(now) or symbol not in self.radar.symbols():
            return False
        ttl = self.cfg.radar_admission_ttl_seconds * 1000
        if not fresh(self.radar.updated_ms, now, ttl) or not fresh(self._filters_updated_ms, now, FILTER_MAX_AGE_MS):
            return False
        f = self.filters.get(symbol)
        return (symbol in self._current_filter_symbols and f is not None
                and f.status == "TRADING" and f.contract_type == "PERPETUAL")

    def _radar_entry_healthy(self, symbol: str, style) -> bool:
        health = self.health_mon.symbol_health(symbol, style.all_tfs)
        top = self.books.top(symbol)
        trade_ts = self.trades.last_ts(symbol)
        book_age = self.books.age_ms(symbol)
        now = now_ms()
        return bool(health.passed and top is not None and trade_ts > 0
                    and book_age is not None and 0 <= book_age <= 8000
                    and 0 <= now - trade_ts <= self.cfg.max_stream_latency_ms
                    and 0 <= top[2] <= self.cfg.max_spread_pct)

    def execution_symbols(self, style_name: Optional[str] = None, now: Optional[int] = None) -> List[str]:
        """Current entry permissions; held/retired positions are NOT entry permissions."""
        now = now if now is not None else now_ms()
        dynamic = [] if style_name not in (None, "day") else [
            s for s in self.admission.symbols(now) if self._radar_symbol_allowed(s, now)]
        return list(dict.fromkeys(list(self.cfg.symbols) + dynamic))

    def _held_symbols(self) -> List[str]:
        # Persisted owned trades survive admission expiry and process restart.
        return list(dict.fromkeys(row["symbol"] for row in self.db.open_trades()))

    def _monitored_symbols(self) -> List[str]:
        return list(dict.fromkeys(self.execution_symbols() + self._held_symbols()))

    def _sync_execution_feeds(self) -> None:
        self.poller.symbols = self._monitored_symbols()
        self.streams.set_symbols(self._stream_symbols())

    def _stream_symbols(self) -> List[str]:
        return list(dict.fromkeys(self._monitored_symbols() + self.radar.symbols()))

    def _refresh_radar(self) -> None:
        if self.cfg.radar_count <= 0:
            return
        if self.cfg.radar_trading_enabled and not self._load_filters():
            with self._trade_lock:
                self.admission.clear()
                self._sync_execution_feeds()
            return
        syms = self.radar.refresh(self.rest, self.filters, self.cfg.symbols, self.cfg.radar_count)
        new = [s for s in syms if self.candles.count(s, RADAR_TF) == 0]
        with self._trade_lock:
            self.admission.revoke_missing(syms)
        self.radar_poller.symbols = list(syms)
        for s in new:                                  # radar symbols need history once; afterwards they are polled
            self._safe(lambda s=s: self.candles.ingest(s, RADAR_TF, self.rest.klines(s, RADAR_TF, limit=200)), f"radar bootstrap {s}")
        self._sync_execution_feeds()
        log.info("radar candidates (admission required for trading): %s", ",".join(syms))

    def radar_scan(self) -> Dict[str, Any]:
        """Analyze Radar and update symbol permissions. This method never sends orders."""
        if self.cfg.radar_trading_enabled:
            with self._trade_lock:
                self.admission.clear()
        self.radar_ranked = tier1(self.radar.candidates(), self.candles)
        deep = select_deep(self.radar_ranked, self.cfg.radar_deep_count)
        for s in deep:                                  # promoted candidates need deeper history once
            required_tfs = DEEP_TFS  # Day Trade only: 5m/15m/1h/4h; no mandatory allowlist
            for tf in required_tfs:
                if self.candles.count(s, tf) < 150:
                    self._safe(lambda s=s, tf=tf: self.candles.ingest(s, tf, self.rest.klines(s, tf, limit=300)), f"radar deep bootstrap {s} {tf}")
        self.deep_poller.symbols = list(deep)
        results: Dict[str, Dict[str, Any]] = {}
        for s in deep:
            an = self.director.analyze(s, DEEP_STYLE)
            res = self._result(an)
            res["status"] = "SCAN_ONLY"
            results[s] = {"ts": an.ts, "direction": an.direction, "approved": an.approved and an.grade in ("A+", "A"), "grade": an.grade, "quality": an.quality,
                          "playbook": res["playbook"]["name"] if res["playbook"] else None, "reasons": an.reasons[:3],
                          "setup": res["setup"] if an.approved else None}
            if an.approved:
                self.telegram.publish_scan(res)
        self.radar_deep = results
        with self._trade_lock:
            current_filters = {s: self.filters[s] for s in self._current_filter_symbols if s in self.filters}
            self.admission.replace(
                self.radar_ranked, results, self.radar.updated_ms, current_filters,
                self._filters_updated_ms, now_ms(),
                lambda symbol: self.health_mon.symbol_health(symbol, STYLES["day"].all_tfs).passed,
            )
            self._sync_execution_feeds()
        return {"ranked": self.radar_ranked, "deep": results,
                "admission": self.admission.snapshot(now_ms())}

    def _account(self, fresh: bool = False):
        ts, acct = self._acct_cache
        if acct is not None and not fresh and time.time() - ts < 5.0:
            return acct
        acct = self.exec.account()
        self._acct_cache = (time.time(), acct)
        return acct

    def _exposure(self) -> List[Dict[str, Any]]:
        return self.lifecycle.exposure() if self.lifecycle else self.db.open_trades()

    def live_gate(self) -> Dict[str, Any]:
        return live_gate_status(self.db, self.cfg)

    def can_trade(self) -> Tuple[bool, str]:
        if self.exec is None:
            return False, f"OBSERVE_ONLY ({self.observe_reason})"
        if not self._preflight_ok:
            return False, "PREFLIGHT_NOT_PASSED"
        if self.cfg.mode == "live":
            gate = self.live_gate()
            if not gate["ok"]:
                return False, "LIVE_GATE_CLOSED (" + "; ".join(r.split(" (")[0] for r in gate["reasons"]) + ")"
        halt = self.risk.halted()
        if halt:
            return False, f"HALTED ({halt})"
        return True, ""

    # ================================================================== alerts + mail
    def alert(self, level: str, message: str) -> None:
        log.log(logging.CRITICAL if level == "CRITICAL" else logging.INFO, "ALERT %s: %s", level, message)
        if level == "CRITICAL":
            self.db.event("CRITICAL", "alert", message)
        elif level == "INFO" and message.split(" ", 1)[0] in ("OPENED", "CLOSED"):
            self.db.event("INFO", "trade_alert", message)

    def _flush_alert_mail(self) -> None:
        if self.cfg.alert_email == "none":
            return
        last_id = int(self.db.kv_get("mail.last_event_id", 0) or 0)
        rows = self.db.query("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 200", (last_id,))
        if not rows:
            return
        wanted = [r for r in rows if r["level"] == "CRITICAL" or (self.cfg.alert_email == "trades" and r["kind"] == "trade_alert")]
        new_last = rows[-1]["id"]
        if not wanted:
            self.db.kv_set("mail.last_event_id", new_last)
            return
        now = time.time()
        with self._mail_lock:
            while self._mail_sent and now - self._mail_sent[0] > 86400:
                self._mail_sent.popleft()
            if (self._mail_sent and now - self._mail_sent[-1] < MAIL_MIN_INTERVAL_S) or len(self._mail_sent) >= MAIL_MAX_PER_DAY:
                return                      # retried on the next maintenance pass (events stay unmailed)
            self._mail_sent.append(now)
        crit = any(r["level"] == "CRITICAL" for r in wanted)
        body = "\n".join(f"[{r['level']}] {iso(r['ts'])} {r['message']}" for r in wanted)
        subject = ("IMPERION KRİTİK UYARI" if crit else "IMPERION işlem bildirimi") + f" | {self.cfg.describe_mode()}"
        ok, detail = self.mailer(self.cfg, subject, body + "\n\nDurum: " + json.dumps(self.risk.status(), default=str), None)
        if ok:
            self.db.kv_set("mail.last_event_id", new_last)
        else:
            log.warning("alert mail failed: %s", detail)

    # ================================================================== evaluation
    def _spread_ok(self, symbol: str, health) -> bool:
        if "spread_ok" in health.checks:
            return bool(health.checks["spread_ok"])
        top = self.books.top(symbol)           # streams not required (dry mode): judge the book if there is one
        return True if top is None else top[2] <= self.cfg.max_spread_pct

    def evaluate(self, symbol: str, style_name: str, hint: Optional[str] = None, source: str = "loop",
                 now: Optional[int] = None, prepared: Optional[Analysis] = None) -> Dict[str, Any]:
        """Full pipeline for one (symbol, style). Opens a trade only if every gate passes; publishes the signal."""
        res = self._evaluate(symbol, style_name, hint, source, now, prepared=prepared)
        self.telegram.publish_signal(res)
        return res

    def _result(self, an: Analysis) -> Dict[str, Any]:
        side = an.direction if an.direction in ("LONG", "SHORT") else (an.playbook.side if an.playbook is not None else None)
        cons = an.consensus.get(side) if side else None
        return {"symbol": an.symbol, "style": an.style, "direction": an.direction, "approved": an.approved, "status": "NO_TRADE",
                "reasons": list(an.reasons), "traded": False, "quality": an.quality, "grade": an.grade,
                "net": cons["net"] if cons else 0.0, "agree": cons["agree"] if cons else [],
                "playbook": an.playbook.to_dict() if an.playbook is not None else None,
                "setup": an.setup.to_dict() if an.setup is not None else None,
                "agents": {k: r.to_dict() for k, r in an.reports.items()},
                "market": {"regime": an.regime.get("regime"), "profile": an.regime.get("profile"), "volatility": an.regime.get("volatility"),
                           "session": an.regime.get("session")},
                "mtf": an.mtf, "analysis_valid": False}

    def _evaluate(self, symbol: str, style_name: str, hint: Optional[str], source: str, now: Optional[int],
                  prepared: Optional[Analysis] = None) -> Dict[str, Any]:
        if style_name != "day":
            raise ValueError("DAY_TRADE_ONLY: scalp and swing are not permitted")
        if symbol not in self.execution_symbols(style_name, now):
            raise ValueError(f"{symbol} is not in the execution universe for {style_name}")
        style = STYLES[style_name]
        now = now if now is not None else now_ms()
        health = self.health_mon.symbol_health(symbol, style.all_tfs, now)
        an = (prepared if prepared is not None else
              self.director.analyze(symbol, style_name, hint, now, data_ok=health.passed, health_quality=health.quality))
        self.last_analysis[(symbol, style_name)] = an
        res = self._result(an)
        self._note_result(an, res)
        if not health.passed:
            res["status"] = "DATA_BLOCKED"
            res["reasons"] = (health.reasons[:6] + res["reasons"])[:10]
            self._store_decision(an, res, hint, force=bool(hint))
            return res
        if an.setup is not None:
            self._record_shadow(an, source)
        if not an.approved or an.grade not in ("A+", "A"):
            if an.approved and an.grade not in ("A+", "A"):
                res["reasons"].append("QUALITY_A_PLUS_OR_A_REQUIRED")
            res["status"] = "BLOCKED" if an.setup is not None else "NO_TRADE"
            self._store_decision(an, res, hint, force=bool(hint) or an.setup is not None)
            return res

        setup, side = an.setup, an.direction
        blockers: List[str] = []
        exposure = self._exposure()
        inventory = self.inventory.assess(exposure, side, symbol)
        book = self.books.get(symbol)
        # Ignore stale book snapshots; stale liquidity is not a valid safety signal.
        if book and self.books.age_ms(symbol, now) is not None and self.books.age_ms(symbol, now) > 8_000:
            book = None
        flow = self.trades.flow_profile(symbol, minutes=5, now=now) if self.trades is not None else None
        if flow and flow.get("available"):
            flow["stacked_imbalances"] = self.trades.stacked_imbalances(symbol, minutes=5, now=now)
        top = self.books.top(symbol) if book else None
        tox = toxicity_score(side, flow, book, top[2] if top else None)
        tox_reason = toxicity_veto(tox["score"])
        res["portfolio_guard"] = {"inventory": inventory, "toxicity": tox}
        if not inventory["allowed"]:
            blockers.append(inventory["reason"])
        # Retain portfolio vetoes while completing AI and data-quality
        # reporting. The combined blocker gate below must pass before
        # can_trade(), sizing or order execution can be reached.
        acct = None
        if self.exec is not None:
            try:
                acct = self._account()
            except Exception as exc:
                blockers.append(f"ACCOUNT_UNAVAILABLE ({type(exc).__name__})")
        equity = self.risk.trading_equity(acct.equity) if acct else self.cfg.trading_capital_usdt
        pre = self.risk.precheck(symbol, style, side, exposure, equity)
        fresh = [v for k, v in health.checks.items() if k.startswith("fresh_")]
        trade_ts = self.trades.last_ts(symbol)
        latency = float(now_ms() - trade_ts) if trade_ts > 0 else None
        dq = data_quality_gate(health_passed=health.passed, completed_candles=bool(fresh) and all(fresh), latency_ms=latency,
                               max_latency_ms=self.cfg.max_stream_latency_ms, spread_ok=self._spread_ok(symbol, health))
        ai = self.learning.opinion(an)
        res.update(ai=ai, data_quality=dq, analysis_valid=bool(dq["passed"]))
        blockers += [r for r in pre["reasons"] if r not in blockers] + dq["reasons"] + ai["reasons"]
        if tox_reason:
            blockers.append(tox_reason)
        if blockers:
            res.update(status="BLOCKED", approved=False)
            res["reasons"] += blockers
            self._store_decision(an, res, hint, force=True)
            return res
        res["status"] = "APPROVED"
        decision_id = self._store_decision(an, res, hint, force=True)
        ok, why_not = self.can_trade()
        if not ok:
            res["status"] = "APPROVED_NOT_TRADED"
            res["reasons"].append(why_not)
            self._set_status(decision_id, res["status"])
            return res
        with self._trade_lock:          # entries are serialised (decision loop + webhook threads)
            # Admission can expire/revoke between analysis and the serialized entry.
            if symbol not in self.execution_symbols(style_name):
                res.update(status="BLOCKED", approved=False)
                res["reasons"].append("RADAR_ADMISSION_EXPIRED_OR_REVOKED")
                self._set_status(decision_id, "BLOCKED_AT_ENTRY")
                return res
            if symbol not in self.cfg.symbols:
                if not self._radar_entry_healthy(symbol, style):
                    res.update(status="BLOCKED", approved=False)
                    res["reasons"].append("RADAR_ENTRY_DATA_UNHEALTHY")
                    self._set_status(decision_id, "BLOCKED_AT_ENTRY")
                    return res
            exposure = self._exposure()
            acct = self._account(fresh=True)
            equity = self.risk.trading_equity(acct.equity)
            pre = self.risk.precheck(symbol, style, side, exposure, equity)
            if not pre["allowed"]:
                res.update(status="BLOCKED", approved=False)
                res["reasons"] += pre["reasons"]
                self._set_status(decision_id, "BLOCKED_AT_ENTRY")
                return res
            try:
                brackets = self.exec.leverage_brackets(symbol)
            except Exception as exc:            # fail closed: no liquidation data from the exchange, no entry
                res.update(status="BLOCKED", approved=False)
                res["reasons"].append(f"LEVERAGE_BRACKETS_UNAVAILABLE ({type(exc).__name__})")
                self._set_status(decision_id, "BLOCKED_SIZING")
                return res
            entry_inventory = self.inventory.assess(exposure, side, symbol)
            if not entry_inventory["allowed"]:
                res.update(status="BLOCKED", approved=False)
                res["reasons"].append(entry_inventory["reason"])
                self._set_status(decision_id, "BLOCKED_AT_ENTRY")
                return res
            entry_inventory["size_multiplier"] = min(inventory["size_multiplier"], entry_inventory["size_multiplier"])
            inventory = entry_inventory
            res["portfolio_guard"]["inventory"] = inventory
            sizing = self.risk.size(setup, self.exec.filters(symbol), equity, acct.available, brackets,
                                    risk_multiplier=inventory["size_multiplier"])
            if not sizing["ok"]:
                res.update(status="BLOCKED", approved=False)
                res["reasons"].append(sizing["reason"])
                self._set_status(decision_id, "BLOCKED_SIZING")
                return res
            # Verify the real rounded, fee-aware order risk against remaining budgets.
            exact = self.risk.precheck(symbol, style, side, exposure, equity,
                                       new_risk_usdt=sizing["risk_usdt"])
            if not exact["allowed"]:
                res.update(status="BLOCKED", approved=False)
                res["reasons"] += exact["reasons"]
                self._set_status(decision_id, "BLOCKED_EXACT_RISK")
                return res
            meta = {"quality": an.quality, "grade": an.grade, "playbook": an.playbook.name if an.playbook else None,
                    "net": res["net"], "agree": res["agree"], "source": source, "hint": hint, "weights_version": an.weights_version,
                    "regime": an.regime.get("regime"), "session": an.regime.get("session"), "ai_p_win": ai.get("p_win"),
                    "snapshot": an.snapshot(), "notes": setup.notes, "core": CORE_VERSION,
                    "inventory_guard": inventory, "toxicity_guard": tox,
                    "execution_universe": "fixed" if symbol in self.cfg.symbols else "radar_admitted"}
            # Remote account/bracket calls may take long enough to expire a lease.
            if symbol not in self.cfg.symbols and (
                    not self._radar_symbol_allowed(symbol, now_ms())
                    or not self._radar_entry_healthy(symbol, style)):
                res.update(status="BLOCKED", approved=False)
                res["reasons"].append("RADAR_FINAL_ENTRY_CHECK_FAILED")
                self._set_status(decision_id, "BLOCKED_AT_ENTRY")
                return res
            trade = self.lifecycle.open_trade(setup, sizing, decision_id, meta)
        res["trade_id"], res["trade_status"] = trade["id"], trade["status"]
        res["traded"] = trade["status"] in ("OPEN", "CLOSING", "CLOSED")
        res["status"] = "TRADED" if res["traded"] else "ENTRY_FAILED"
        self._set_status(decision_id, res["status"])
        return res

    def _store_decision(self, an: Analysis, res: Dict[str, Any], hint: Optional[str], force: bool) -> Optional[int]:
        key = (an.symbol, an.style)
        now = now_ms()
        if not force and now - self._last_stored.get(key, 0) < NO_TRADE_LOG_EVERY_MS:
            return None
        self._last_stored[key] = now
        detail = {"agents": res["agents"], "playbook": res["playbook"], "market": res["market"], "consensus": an.consensus,
                  "maps": an.maps, "ai": res.get("ai")}
        return self.db.add_decision({
            "ts": now, "symbol": an.symbol, "style": an.style, "trigger_tf": STYLES[an.style].trigger_tf, "direction": an.direction,
            "approved": res["status"] in ("APPROVED", "APPROVED_NOT_TRADED", "TRADED", "ENTRY_FAILED"), "status": res["status"],
            "net_score": res["net"], "quality": an.quality, "reasons": res["reasons"][:14], "evidence": detail,
            "setup": res["setup"], "weights_version": an.weights_version, "hint": hint, "data_ok": an.data_ok,
        })

    def _set_status(self, decision_id: Optional[int], status: str) -> None:
        if decision_id:
            self.db.execute("UPDATE decisions SET status=? WHERE id=?", (status, decision_id))

    def _record_shadow(self, an: Analysis, source: str) -> None:
        s, style = an.setup, STYLES[an.style]
        recent = self.db.one("SELECT 1 AS x FROM shadows WHERE symbol=? AND style=? AND side=? AND ts>?",
                             (an.symbol, an.style, s.side, now_ms() - style.cooldown_minutes * 60_000))
        if recent:
            return
        meta = {"playbook": s.playbook, "grade": an.grade, "quality": an.quality, "approved": an.approved,
                "regime": an.regime.get("regime"), "session": an.regime.get("session"), "volatility": an.regime.get("volatility"),
                "profile": an.regime.get("profile")}
        self.shadows.record(None, an.symbol, an.style, s.side, s.entry, s.stop, s.tp1, s.tp2, an.snapshot(), source, meta=meta)

    def _note_result(self, an: Analysis, res: Dict[str, Any]) -> None:
        self.last_decision_ms = now_ms()
        prev = self._last_result.get(an.symbol)
        directional = an.direction in ("LONG", "SHORT")
        if directional or prev is None or not prev.get("directional") or now_ms() - prev["ts"] > 6 * 3600_000:
            self._last_result[an.symbol] = {"direction": an.direction, "style": an.style, "ts": now_ms(), "directional": directional,
                                            "quality": an.quality if directional else None, "grade": an.grade if directional else "",
                                            "playbook": res["playbook"]["name"] if res["playbook"] and directional else None,
                                            "regime": an.regime.get("regime"), "session": an.regime.get("session")}

    # ================================================================== loops
    def _loop_candles(self) -> None:
        while not self._stop.wait(1.0):
            self.poller.poll_due()
            self.radar_poller.poll_due()
            self.deep_poller.poll_due()

    def decision_pass(self) -> int:
        """Collect every fresh Day Trade candidate first; execute A+ before A.

        The analysis stage cannot place orders. Every final entry is still
        rechecked under the existing trade lock and all exchange safety gates.
        """
        now = now_ms()
        self._sync_execution_feeds()
        pending = []
        for style_name in self.cfg.active_styles:
            tf = STYLES[style_name].trigger_tf
            expected = last_closed_open(now, tf)
            for sym in self.execution_symbols(style_name, now):
                have = self.candles.last_open(sym, tf)
                if have is None or have < expected:
                    continue
                key = (sym, style_name)
                if self._last_eval.get(key) == have:
                    continue
                self._last_eval[key] = have
                prepared = None
                quality = 0.0
                rank = 2  # non-tradable results are still evaluated and reported
                try:
                    health = self.health_mon.symbol_health(sym, STYLES[style_name].all_tfs, now)
                    prepared = self.director.analyze(
                        sym, style_name, None, now,
                        data_ok=health.passed, health_quality=health.quality,
                    )
                    if (health.passed and prepared.approved and prepared.setup is not None
                            and prepared.direction in ("LONG", "SHORT")):
                        rank = {"A+": 0, "A": 1}.get(prepared.grade, 2)
                        quality = (float(prepared.quality)
                                   if isinstance(prepared.quality, (int, float)) and math.isfinite(prepared.quality)
                                   else 0.0)
                except Exception as exc:
                    # A failed preliminary analysis must never grant permission.
                    # Let the normal error-reporting path attempt evaluation once.
                    self._throttled_error(f"prepare-{sym}",
                                          f"Pre-analysis {sym}/{style_name}: {type(exc).__name__}: {exc}")
                pending.append((rank, -quality, sym, style_name, prepared))
        pending.sort(key=lambda row: (row[0], row[1], row[2]))
        for _, _, sym, style_name, prepared in pending:
            self._safe(lambda sym=sym, style_name=style_name, prepared=prepared:
                       self.evaluate(sym, style_name, source="loop", now=now, prepared=prepared),
                       f"evaluate {sym}/{style_name}")
        return len(pending)

    def _loop_decision(self) -> None:
        self._stop.wait(5.0)
        while not self._stop.wait(2.0):
            self.decision_pass()

    def manage_pass(self) -> None:
        if self.exec is None or self.lifecycle is None:
            return
        if isinstance(self.exec, SimExecutor):
            for sym in list(self.exec.positions()):
                px = self.trades.last_price(sym) or self.candles.last_price(sym)
                if px:
                    self.exec.process_price(sym, px)
        self.lifecycle.manage()
        self._sync_execution_feeds()
        if time.time() - self._timers.get("equity", 0) >= 15:
            self._timers["equity"] = time.time()
            acct = self._account(fresh=True)
            self.risk.update_equity(acct.equity)
            if time.time() - self._timers.get("equity_row", 0) >= 300:
                self._timers["equity_row"] = time.time()
                self.db.add_equity(acct.equity, acct.available, acct.unrealized, self.exec.mode)

    def _loop_manage(self) -> None:
        while not self._stop.is_set():
            self._manage_wake.wait(4.0)             # 4 s cycle, or immediately when the user stream reports a fill
            self._manage_wake.clear()
            if self._stop.is_set():
                break
            self._safe(self.manage_pass, "manage")

    def maintenance_pass(self) -> None:
        if self._due("derivatives", 60):
            self._safe(lambda: self.deriv.refresh(self.rest), "derivatives")
        if self._due("shadows", 60):
            self._safe(lambda: self.shadows.resolve_open(round_trip_cost_pct(self.cfg)), "shadow resolve")
        if self._due("radar", 600):
            self._safe(self._refresh_radar, "radar")
        if self._due("radar_scan", 900):
            self._safe(self.radar_scan, "radar scan")
        if self._due("filters", 6 * 3600):
            self._safe(self._load_filters, "filters")
        if self.exec is not None and not self._preflight_ok and self._due("preflight", 600):
            self._safe(self.run_preflight, "preflight")
        if self._due("ai", 3600):
            self._safe(self._ai_daily, "learning cycle")
        if self._due("report", 30):
            self._safe(self._report_pass, "daily report")
        if self._due("mail", 20):
            self._safe(self._flush_alert_mail, "alert mail")
        if self.tg is not None:
            if self._due("tg_events", 5):
                self._safe(self.telegram.flush_events, "telegram alerts")
            if self.cfg.telegram_digest_hours > 0 and self._due("tg_digest_check", 60):
                self._safe(self.telegram.digest, "telegram digest")
        if self._due("rate_watch", 30):
            self._safe(self._watch_rate_limits, "rate-limit watch")
        if self._due("prune", 6 * 3600):
            self._safe(lambda: self.db.prune(), "prune")

    def _watch_rate_limits(self) -> None:
        """REST weight monitoring: warn early, escalate on 429/418 (a 418 IP ban stops ALL traffic)."""
        st = self.guard.stats()
        prev = self._rate_seen
        if st["count_418"] > prev.get("count_418", 0):
            self.db.event("CRITICAL", "rate_limit", f"Binance IP ban (HTTP 418): all REST traffic paused for {st['banned_s']:.0f}s", st)
        elif st["count_429"] > prev.get("count_429", 0):
            self.db.event("WARNING", "rate_limit", f"Binance rate limit (HTTP 429): cooldown {st['cooldown_s']:.0f}s", st)
        elif st["used_weight"] >= 0.8 * st["soft_limit"] and time.time() - self._timers.get("rate_warn", 0) > 600:
            self._timers["rate_warn"] = time.time()
            self.db.event("WARNING", "rate_limit", f"REST weight {st['used_weight']}/min is close to the local limit {st['soft_limit']}", st)
        self._rate_seen = {"count_418": st["count_418"], "count_429": st["count_429"]}

    def _loop_maintenance(self) -> None:
        self._stop.wait(10.0)
        while not self._stop.wait(5.0):
            self.maintenance_pass()

    def _ai_daily(self) -> None:
        day = utc_day()
        if self.db.kv_get("ai.last_run_day") == day:
            return
        self.db.kv_set("ai.last_run_day", day)
        self.learning.cycle()

    def _report_pass(self) -> None:
        day = daily.report_due(self.cfg, self.db)
        if not day or time.time() < self._timers.get("report_retry_at", 0):
            return
        ok, detail = self.send_report()
        self.db.mark_report(day, ok, detail)
        if not ok:
            self._timers["report_retry_at"] = time.time() + 1800
            self.db.event("ERROR", "report_failed", f"daily report not sent: {detail}")

    # ================================================================== API for HTTP / CLI / Telegram
    def send_report(self) -> Tuple[bool, str]:
        data = daily.gather(self.cfg, self.db, self.snapshot())
        subject, text, html_body = daily.render(data)
        ok, detail = self.mailer(self.cfg, subject, text, html_body)
        if self.tg is not None:                 # the report also goes to Telegram; either channel counts as delivered
            self.tg.send(f"📬 <b>{tgm.esc(subject)}</b>\n<pre>{tgm.esc(text[:3400])}</pre>")
            ok, detail = True, (detail + "+telegram") if ok else "telegram"
        return ok, detail

    def preview_report(self) -> Tuple[str, str]:
        subject, text, _ = daily.render(daily.gather(self.cfg, self.db, self.snapshot()))
        return subject, text

    def radar_snapshot(self) -> List[Dict[str, Any]]:
        """Scan-only candidates with a 1h structure/trend reading. Never used for orders."""
        out = []
        now = now_ms()
        for m in self.radar.candidates():
            b = cached_tf_bias(self.candles, m["symbol"], RADAR_TF)
            out.append({**m, "bias_1h": b["bias"], "structure_1h": b["structure"],
                        "whale_net_15m": self.trades.whale_stats(m["symbol"], 900_000, now)["net_usdt"]})
        return out

    def snapshot(self) -> Dict[str, Any]:
        now = now_ms()
        symbols: Dict[str, Any] = {}
        for s in self._monitored_symbols():
            last = self._last_result.get(s) or {}
            mtf = mtf_view(self.candles, s, TIMEFRAMES)
            flow = live_flow(self.trades, self.books, s, now)
            d = self.deriv.get(s)
            symbols[s] = {
                "price": self.trades.last_price(s) or self.candles.last_price(s), "mtf": mtf["direction"], "mtf_net": mtf["net"],
                "regime": last.get("regime", "-"), "direction": last.get("direction", "-"), "grade": last.get("grade") or "-",
                "playbook": last.get("playbook"), "quality": last.get("quality"),
                "whale_net_15m": flow.get("whale_net_15m"), "book_imbalance": flow.get("book_imbalance"),
                "funding_pct": d["funding"] * 100 if d else None,
            }
        lead = mtf_view(self.candles, "BTCUSDT", ("4h", "1h")) if "BTCUSDT" in self.cfg.symbols else None
        br = breadth(self.candles, self.cfg.symbols, "1h")
        return {"symbols": symbols, "radar": self.radar_snapshot(), "radar_deep": self.radar_deep, "ai": self.learning.summary(), "risk": self.risk.status(),
                "market": {"session": session(now), "breadth": br.get("share"), "breadth_direction": br.get("direction", "-"),
                           "leader_bias": lead["direction"] if lead else "-"},
                "health": {"ws": self.streams.status(), "trading_enabled": self.can_trade()[0]}}

    def health(self) -> Dict[str, Any]:
        ok, _ = self.can_trade()
        # Readiness is distinct from process liveness. A not-yet-started engine
        # must not be reported healthy to deployment orchestrators.
        return {"ok": bool(self._ready), "ready": bool(self._ready), "version": VERSION, "core": f"{CORE_NAME} {CORE_VERSION}", "mode": self.cfg.describe_mode(),
                "uptime_s": int((now_ms() - self._started_ms) / 1000), "trading_enabled": ok, "ws_healthy": self.streams.healthy(),
                "halted": bool(self.risk.halted()),
                "last_decision_age_s": int((now_ms() - self.last_decision_ms) / 1000) if self.last_decision_ms else None}

    def status(self) -> Dict[str, Any]:
        ok, why = self.can_trade()
        try:
            acct = self._account() if self.exec is not None else None
        except Exception as exc:
            acct = None
            why = f"{why} account error: {exc}"
        now = now_ms()
        style0 = STYLES[self.cfg.active_styles[0]]
        sym_health = {}
        for s in self._monitored_symbols():
            h = self.health_mon.symbol_health(s, style0.all_tfs, now)
            sym_health[s] = {"passed": h.passed, "failed": [k for k, v in h.checks.items() if not v][:5]}
        return {
            "version": VERSION, "core": f"{CORE_NAME} {CORE_VERSION}", "mode": self.cfg.describe_mode(), "trading_enabled": ok, "why_not": why,
            "equity": {"equity": acct.equity, "available": acct.available, "unrealized": acct.unrealized} if acct else None,
            "risk": self.risk.status(acct.equity if acct else None), "open_trades": self._exposure(),
            "preflight": self.preflight_result, "streams": self.streams.status(), "symbol_health": sym_health,
            "telegram": self.tg.status() if self.tg else None, "user_stream": self.user_stream.status() if self.user_stream else None,
            "live_gate": self.live_gate(), "rate_limit": self.guard.stats(),
            "radar": [m["symbol"] for m in self.radar.markets], "radar_tiers": {"ranked": self.radar_ranked[:30], "deep": self.radar_deep},
            "ai": self.learning.summary(),
            "execution_universe": {"fixed": list(self.cfg.symbols),
                                   "radar": self.admission.snapshot(now),
                                   "monitored": self._monitored_symbols()},
            "last_decisions": {s: {k: v for k, v in r.items() if k != "directional"} for s, r in self._last_result.items()},
            "recent_events": self.db.events_since(now - 6 * 3600_000, ("WARNING", "ERROR", "CRITICAL"))[-15:],
        }

    def halt(self, reason: str) -> None:
        self.risk.halt(reason, "admin")

    def resume(self) -> None:
        equity = None
        if self.exec is not None:
            try:
                equity = self._account(fresh=True).equity
            except Exception:
                equity = None
        self.risk.resume(equity=equity, by="admin")
        if equity is not None:
            self.risk.update_equity(equity)

    def flatten(self) -> List[str]:
        if self.lifecycle is None:
            return []
        self.risk.halt("flatten requested by admin", "admin")
        return self.lifecycle.flatten_all("admin flatten")

    def ai_summary(self) -> Dict[str, Any]:
        return {**self.learning.summary(), "last_report": self.db.kv_get("ai.last_report")}

    def ai_run(self) -> Dict[str, Any]:
        self.shadows.resolve_open(round_trip_cost_pct(self.cfg))
        return self.learning.cycle()

    def ai_approve(self, version: int) -> bool:
        return self.learning.learner.approve(version)

    def ai_rollback(self) -> int:
        return self.learning.learner.rollback()
