"""Central configuration. Everything operational comes from environment variables.

Safety model: real orders require ALL of
  MODE=live  +  API key/secret  +  LIVE_TRADING_CONFIRM exact phrase  +  passing preflight
and are always blocked while the kill switch / halt flag is set. The default mode is `dry` (simulated fills).
"""
from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

from .util import env_bool

VERSION = "17.0.0"
CORE_NAME = "IMPERION Director Unified"
CORE_VERSION = "agents-1-v17-unified-institutional"  # isolate V16 learning/shadow data from prior scoring semantics

# Fixed execution universe (16 USDT perpetuals). Order = reporting order.
FIXED_SYMBOLS: Tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT",
    "BCHUSDT", "SUIUSDT", "NEARUSDT", "APTUSDT",
)
DYNAMIC_SCAN_LIMIT = 30            # candidate cap; execution requires separate admission
MAX_SYMBOLS = 40
TIMEFRAMES: Tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
LIVE_CONFIRM_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"
GRADES = ("A+", "A", "B")


@dataclass(frozen=True)
class StyleConfig:
    """Timeframe roles and trade-management limits of one trading style."""
    name: str
    htf_bias: Tuple[str, ...]      # higher timeframes: direction of the market
    setup_tf: str                  # where the playbook (structure, liquidity, zones) is read
    trigger_tf: str                # where the entry is confirmed (candles, micro structure shifts, order flow)
    min_quality: float             # 0..100 quality needed for a signal (grade B)
    min_rr: float                  # final target must pay at least this many R
    max_rr: float                  # final target is capped here (liquidity beyond is not "expected")
    max_hold_minutes: int
    cooldown_minutes: int
    max_stop_atr: float            # widest acceptable stop in ATR(setup TF) multiples

    @property
    def all_tfs(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(self.htf_bias + (self.setup_tf, self.trigger_tf)))


STYLES: Dict[str, StyleConfig] = {
    "scalp": StyleConfig("scalp", ("1h", "15m"), "5m", "1m", 68.0, 1.8, 3.0, 120, 20, 2.5),
    "day": StyleConfig("day", ("4h", "1h"), "15m", "5m", 70.0, 2.0, 4.0, 24 * 60, 90, 3.0),
    "swing": StyleConfig("swing", ("1w", "1d"), "4h", "1h", 72.0, 2.5, 6.0, 14 * 24 * 60, 720, 3.5),
}


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


def _strict_i(name: str, default: int) -> int:
    value = _f(name, default)
    if not math.isfinite(value) or int(value) != value:
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _s(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    mode: str = "dry"                        # dry | testnet | live
    live_confirm: str = ""
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    allow_unverified_key_permissions: bool = False

    rest_base: str = "https://fapi.binance.com"      # public market data
    trade_base: str = "https://fapi.binance.com"     # signed trading endpoints
    ws_base: str = "wss://fstream.binance.com"
    spot_base: str = "https://api.binance.com"       # only for the API-key permission check

    symbols: Tuple[str, ...] = FIXED_SYMBOLS
    active_styles: Tuple[str, ...] = ("scalp", "day", "swing")
    radar_count: int = DYNAMIC_SCAN_LIMIT
    whale_floor_usdt: float = 50_000.0

    trading_capital_usdt: float = 100.0
    risk_per_trade_pct: float = 0.25
    max_concurrent_positions: int = 1
    max_open_risk_pct: float = 0.50
    max_daily_new_risk_pct: float = 1.0
    daily_loss_limit_pct: float = 1.5
    max_drawdown_pct: float = 5.0
    consecutive_loss_pause: int = 3
    loss_pause_minutes: int = 240
    leverage: int = 2
    max_leverage: int = 2
    taker_fee_pct: float = 0.05
    slippage_pct: float = 0.03
    max_cost_to_risk: float = 0.20
    max_risk_overshoot: float = 1.25
    max_spread_pct: float = 0.06
    min_equity_usdt: float = 20.0

    kill_switch: bool = False
    require_streams: bool = True             # block trading unless the trade/depth WebSocket is healthy (forced on outside dry)
    storage_dir: str = "storage"
    port: int = 8080
    admin_token: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = field(default="", repr=False)
    smtp_pass: str = field(default="", repr=False)
    smtp_ssl: bool = False
    mail_from: str = ""
    mail_to: str = ""
    report_hour: int = 8
    report_tz: str = "Europe/Istanbul"
    alert_email: str = "critical"            # critical | trades | none

    ai_min_samples: int = 60
    ai_context_filter: bool = True           # stand aside where the shadow record PROVES a negative edge (can only block)
    ai_veto: bool = True                     # a walk-forward VALIDATED win model may block low-probability setups
    ai_min_win_prob: float = 0.30

    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    telegram_signals: str = "approved"       # approved | setups | off
    telegram_min_grade: str = "B"            # B | A | A+  (weakest grade that is published)
    telegram_trades: bool = True
    telegram_digest_hours: float = 4.0
    telegram_commands: bool = True

    entry_mode: str = "market"               # market | maker (post-only limit at the touch)
    entry_maker_wait_s: float = 8.0
    entry_maker_fallback: str = "market"     # market | skip
    trail_atr_mult: float = 2.0              # after TP1 the stop trails the mark price by this many ATR; 0 = off
    funding_block_minutes: int = 10
    max_adverse_funding_pct: float = 0.05
    user_stream: bool = False
    user_stream_path: str = "private/ws"

    live_gate_max_age_days: int = 30         # testnet evidence older than this no longer opens the live gate
    testnet_evidence_secret: str = field(default="", repr=False)   # HMAC key that signs / verifies testnet evidence
    radar_deep_count: int = 5                # radar candidates promoted to a full (scan-only) Director analysis
    telegram_radar: bool = True              # publish scan-only radar setups (never traded)
    regime_adaptive: bool = True             # regime-dependent agent weights and thresholds
    max_stream_latency_ms: float = 8000.0
    log_level: str = "INFO"

    # Radar admission is separate from fixed symbols and defaults to OFF.
    radar_trading_enabled: bool = False
    radar_trade_allowlist: Tuple[str, ...] = ()
    radar_max_active: int = 3
    radar_min_quote_volume: float = 10_000_000.0
    radar_admission_ttl_seconds: int = 1200

    # ---- derived -------------------------------------------------------
    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def live_confirmed(self) -> bool:
        return self.live_confirm == LIVE_CONFIRM_PHRASE

    @property
    def orders_possible(self) -> bool:
        """True when configuration alone would allow sending orders to Binance (testnet or live)."""
        if self.mode == "dry":
            return False
        return self.has_credentials and (self.mode == "testnet" or self.live_confirmed)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def db_path(self) -> str:
        return str(Path(self.storage_dir) / "imperion.db")

    def describe_mode(self) -> str:
        if self.mode == "dry":
            return "DRY (simulated fills, real market data)"
        if self.mode == "testnet":
            return "TESTNET" if self.has_credentials else "TESTNET (no keys: observe-only)"
        if not self.has_credentials:
            return "LIVE (NO API KEYS: observe-only)"
        if not self.live_confirmed:
            return "LIVE (LIVE_TRADING_CONFIRM missing: observe-only)"
        return "LIVE (real money)"

    def validate(self) -> None:
        if not isinstance(self.radar_trading_enabled, bool):
            raise ValueError("RADAR_TRADING_ENABLED must be boolean")
        if not isinstance(self.radar_trade_allowlist, tuple):
            raise ValueError("RADAR_TRADE_ALLOWLIST must be a tuple of symbols")
        if len(set(self.radar_trade_allowlist)) != len(self.radar_trade_allowlist):
            raise ValueError("RADAR_TRADE_ALLOWLIST contains duplicates")
        for symbol in self.radar_trade_allowlist:
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,24}USDT", symbol):
                raise ValueError("Invalid RADAR_TRADE_ALLOWLIST symbol")
        for name, value, low, high in (
            ("RADAR_MAX_ACTIVE", self.radar_max_active, 1, 10),
            ("RADAR_ADMISSION_TTL_SECONDS", self.radar_admission_ttl_seconds, 60, 1800),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in {low}..{high}")
        if (isinstance(self.radar_min_quote_volume, bool)
                or not isinstance(self.radar_min_quote_volume, (int, float))
                or not math.isfinite(self.radar_min_quote_volume) or self.radar_min_quote_volume <= 0):
            raise ValueError("RADAR_MIN_QUOTE_VOLUME must be finite and positive")
        if self.radar_trading_enabled:
            if not self.radar_trade_allowlist or self.radar_count < 1 or self.radar_deep_count < 1:
                raise ValueError("Radar trading requires an allowlist and enabled scanning/deep analysis")
            if "day" not in self.active_styles:
                raise ValueError("Radar trading currently requires the day style")
            if len(self.symbols) + self.radar_max_active > MAX_SYMBOLS:
                raise ValueError("Combined execution universe exceeds MAX_SYMBOLS")
        if self.mode not in ("live", "testnet", "dry"):
            raise ValueError("MODE must be dry, testnet or live")
        bad = [s for s in self.active_styles if s not in STYLES]
        if bad or not self.active_styles:
            raise ValueError(f"ACTIVE_STYLES must be a non-empty subset of {sorted(STYLES)} (got {list(self.active_styles)})")
        if not 0 < self.risk_per_trade_pct <= 5:
            raise ValueError("RISK_PER_TRADE_PCT must be in (0, 5]")
        if not 1 <= self.leverage <= self.max_leverage <= 20:
            raise ValueError("LEVERAGE must be 1..MAX_LEVERAGE and MAX_LEVERAGE <= 20")
        if self.trading_capital_usdt <= 0:
            raise ValueError("TRADING_CAPITAL_USDT must be positive")
        if self.max_concurrent_positions < 1:
            raise ValueError("MAX_CONCURRENT_POSITIONS must be >= 1")
        if self.alert_email not in ("critical", "trades", "none"):
            raise ValueError("ALERT_EMAIL must be critical, trades or none")
        if self.telegram_signals not in ("approved", "setups", "off"):
            raise ValueError("TELEGRAM_SIGNALS must be approved, setups or off")
        if self.telegram_min_grade not in GRADES:
            raise ValueError(f"TELEGRAM_MIN_GRADE must be one of {GRADES}")
        if self.telegram_digest_hours < 0:
            raise ValueError("TELEGRAM_DIGEST_HOURS must be >= 0")
        if self.entry_mode not in ("market", "maker"):
            raise ValueError("ENTRY_MODE must be market or maker")
        if self.entry_maker_fallback not in ("market", "skip"):
            raise ValueError("ENTRY_MAKER_FALLBACK must be market or skip")
        if not 1 <= self.entry_maker_wait_s <= 60:
            raise ValueError("ENTRY_MAKER_WAIT_S must be 1..60")
        if self.trail_atr_mult < 0 or (0 < self.trail_atr_mult < 0.5):
            raise ValueError("TRAIL_ATR_MULT must be 0 (off) or >= 0.5")
        if self.funding_block_minutes < 0 or self.max_adverse_funding_pct < 0:
            raise ValueError("FUNDING_BLOCK_MINUTES / MAX_ADVERSE_FUNDING_PCT must be >= 0")
        if not 0 < self.ai_min_win_prob < 1:
            raise ValueError("AI_MIN_WIN_PROB must be in (0, 1)")
        if self.ai_min_samples < 20:
            raise ValueError("AI_MIN_SAMPLES must be >= 20")
        if self.max_stream_latency_ms < 1000:
            raise ValueError("MAX_STREAM_LATENCY_MS must be >= 1000")
        if not 0 <= self.report_hour <= 23:
            raise ValueError("REPORT_HOUR must be 0..23")
        if not 1 <= len(self.symbols) <= MAX_SYMBOLS:
            raise ValueError(f"SYMBOLS must list 1..{MAX_SYMBOLS} symbols")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("SYMBOLS must not contain duplicates")
        for s in self.symbols:
            if not s.isalnum() or not s.endswith("USDT"):
                raise ValueError(f"Invalid symbol {s}")
        if not 1 <= self.live_gate_max_age_days <= 90:
            raise ValueError("LIVE_GATE_MAX_AGE_DAYS must be 1..90")
        if self.testnet_evidence_secret and len(self.testnet_evidence_secret) < 32:
            raise ValueError("TESTNET_EVIDENCE_SECRET must be at least 32 characters")
        if not 0 <= self.radar_deep_count <= 10:
            raise ValueError("RADAR_DEEP_COUNT must be 0..10")
        if not 0 <= self.radar_count <= DYNAMIC_SCAN_LIMIT:
            raise ValueError(f"RADAR_COUNT must be 0..{DYNAMIC_SCAN_LIMIT}")


def load_settings(env: Optional[Dict[str, str]] = None) -> Settings:
    """Build Settings from os.environ (or a provided mapping for tests)."""
    if env is not None:
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            return load_settings()
        finally:
            os.environ.clear()
            os.environ.update(saved)

    mode = _s("MODE", "dry").lower()
    prod_rest, testnet_rest = "https://fapi.binance.com", "https://demo-fapi.binance.com"
    prod_ws, testnet_ws = "wss://fstream.binance.com", "wss://demo-fstream.binance.com"
    rest_default = testnet_rest if mode == "testnet" else prod_rest
    ws_default = testnet_ws if mode == "testnet" else prod_ws
    symbols = tuple(x.strip().upper() for x in _s("SYMBOLS", ",".join(FIXED_SYMBOLS)).split(",") if x.strip())
    styles = tuple(x.strip().lower() for x in _s("ACTIVE_STYLES", "scalp,day,swing").split(",") if x.strip())

    s = Settings(
        radar_trading_enabled=env_bool("RADAR_TRADING_ENABLED", False),
        radar_trade_allowlist=tuple(x.strip().upper() for x in _s("RADAR_TRADE_ALLOWLIST").split(",") if x.strip()),
        radar_max_active=_strict_i("RADAR_MAX_ACTIVE", 3),
        radar_min_quote_volume=_f("RADAR_MIN_QUOTE_VOLUME", 10_000_000.0),
        radar_admission_ttl_seconds=_strict_i("RADAR_ADMISSION_TTL_SECONDS", 1200),
        mode=mode,
        live_confirm=_s("LIVE_TRADING_CONFIRM"),
        api_key=_s("BINANCE_API_KEY"),
        api_secret=_s("BINANCE_API_SECRET"),
        allow_unverified_key_permissions=env_bool("ALLOW_UNVERIFIED_KEY_PERMISSIONS", False),
        rest_base=_s("BINANCE_REST_BASE", rest_default).rstrip("/"),
        trade_base=_s("BINANCE_TRADE_BASE", rest_default).rstrip("/"),
        ws_base=_s("BINANCE_WS_BASE", ws_default).rstrip("/"),
        spot_base=_s("BINANCE_SPOT_BASE", "https://api.binance.com").rstrip("/"),
        symbols=symbols,
        active_styles=styles,
        radar_count=_i("RADAR_COUNT", DYNAMIC_SCAN_LIMIT),
        whale_floor_usdt=_f("WHALE_FLOOR_USDT", 50_000.0),
        trading_capital_usdt=_f("TRADING_CAPITAL_USDT", 100.0),
        risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.25),
        max_concurrent_positions=_i("MAX_CONCURRENT_POSITIONS", 1),
        max_open_risk_pct=_f("MAX_OPEN_RISK_PCT", 0.50),
        max_daily_new_risk_pct=_f("MAX_DAILY_NEW_RISK_PCT", 1.0),
        daily_loss_limit_pct=_f("DAILY_LOSS_LIMIT_PCT", 1.5),
        max_drawdown_pct=_f("MAX_DRAWDOWN_PCT", 5.0),
        consecutive_loss_pause=_i("CONSECUTIVE_LOSS_PAUSE", 3),
        loss_pause_minutes=_i("LOSS_PAUSE_MINUTES", 240),
        leverage=_i("LEVERAGE", 2),
        max_leverage=_i("MAX_LEVERAGE", 2),
        taker_fee_pct=_f("TAKER_FEE_PCT", 0.05),
        slippage_pct=_f("SLIPPAGE_PCT", 0.03),
        max_cost_to_risk=_f("MAX_COST_TO_RISK", 0.20),
        max_risk_overshoot=_f("MAX_RISK_OVERSHOOT", 1.25),
        max_spread_pct=_f("MAX_SPREAD_PCT", 0.06),
        min_equity_usdt=_f("MIN_EQUITY_USDT", 20.0),
        kill_switch=env_bool("KILL_SWITCH", False),
        require_streams=env_bool("REQUIRE_STREAMS", True) or mode != "dry",
        storage_dir=_s("IMPERION_STORAGE_DIR", "storage") or "storage",
        port=_i("PORT", 8080),
        admin_token=_s("ADMIN_TOKEN"),
        webhook_secret=_s("TRADINGVIEW_WEBHOOK_SECRET"),
        smtp_host=_s("SMTP_HOST"),
        smtp_port=_i("SMTP_PORT", 587),
        smtp_user=_s("SMTP_USER"),
        smtp_pass=_s("SMTP_PASS"),
        smtp_ssl=env_bool("SMTP_SSL", False),
        mail_from=_s("MAIL_FROM") or _s("SMTP_USER"),
        mail_to=_s("MAIL_TO"),
        report_hour=_i("REPORT_HOUR", 8),
        report_tz=_s("REPORT_TZ", "Europe/Istanbul"),
        alert_email=_s("ALERT_EMAIL", "critical").lower(),
        ai_min_samples=_i("AI_MIN_SAMPLES", 60),
        ai_context_filter=env_bool("AI_CONTEXT_FILTER", True),
        ai_veto=env_bool("AI_VETO", True),
        ai_min_win_prob=_f("AI_MIN_WIN_PROB", 0.30),
        telegram_token=_s("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_s("TELEGRAM_CHAT_ID"),
        telegram_signals=_s("TELEGRAM_SIGNALS", "approved").lower(),
        telegram_min_grade=_s("TELEGRAM_MIN_GRADE", "B").upper(),
        telegram_trades=env_bool("TELEGRAM_TRADES", True),
        telegram_digest_hours=_f("TELEGRAM_DIGEST_HOURS", 4.0),
        telegram_commands=env_bool("TELEGRAM_COMMANDS", True),
        entry_mode=_s("ENTRY_MODE", "market").lower(),
        entry_maker_wait_s=_f("ENTRY_MAKER_WAIT_S", 8.0),
        entry_maker_fallback=_s("ENTRY_MAKER_FALLBACK", "market").lower(),
        trail_atr_mult=_f("TRAIL_ATR_MULT", 2.0),
        funding_block_minutes=_i("FUNDING_BLOCK_MINUTES", 10),
        max_adverse_funding_pct=_f("MAX_ADVERSE_FUNDING_PCT", 0.05),
        user_stream=env_bool("USER_STREAM", False),
        user_stream_path=_s("USER_STREAM_PATH", "private/ws").strip("/"),
        live_gate_max_age_days=_i("LIVE_GATE_MAX_AGE_DAYS", 30),
        testnet_evidence_secret=_s("TESTNET_EVIDENCE_SECRET"),
        radar_deep_count=_i("RADAR_DEEP_COUNT", 5),
        telegram_radar=env_bool("TELEGRAM_RADAR", True),
        regime_adaptive=env_bool("REGIME_ADAPTIVE", True),
        max_stream_latency_ms=_f("MAX_STREAM_LATENCY_MS", 8000.0),
        log_level=_s("LOG_LEVEL", "INFO"),
    )
    s.validate()
    return s
