"""Telegram side of the engine: signal publishing, event forwarding, periodic digest and chat commands."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict

from ..analysis.mtf import mtf_view
from ..config import CORE_NAME, CORE_VERSION, GRADES, STYLES, TIMEFRAMES, VERSION
from ..notify import telegram as tgm
from ..util import iso, now_ms

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

EVENT_KINDS = ("trade_alert", "break_even", "trail_stop", "loss_pause", "resume", "rate_limit", "gate_approval", "gate_revoked")
APPROVED_STATES = ("TRADED", "APPROVED", "APPROVED_NOT_TRADED", "ENTRY_FAILED")


def grade_ok(grade: str, minimum: str) -> bool:
    """A+ > A > B. Anything else (C, empty) never passes."""
    return grade in GRADES and GRADES.index(grade) <= GRADES.index(minimum)


class TelegramService:
    def __init__(self, engine: "Engine"):
        self.e = engine
        self._sent: Dict[tuple, int] = {}

    @property
    def tg(self):
        return self.e.tg

    def on_start(self) -> None:
        if self.tg is None:
            return
        db, cfg = self.e.db, self.e.cfg
        if db.kv_get("tg.last_event_id") is None:      # first run with Telegram: do not replay the event history
            db.kv_set("tg.last_event_id", (db.one("SELECT COALESCE(MAX(id),0) AS m FROM events") or {"m": 0})["m"])
        self.tg.send(f"🚀 <b>IMPERION v{tgm.esc(VERSION)}</b> · {tgm.esc(CORE_NAME)} {tgm.esc(CORE_VERSION)} başladı\n"
                     f"Mod: {tgm.esc(cfg.describe_mode())}\nSemboller: {len(cfg.symbols)} · Stiller: {tgm.esc(', '.join(cfg.active_styles))} · "
                     f"Tarama listesi: {cfg.radar_count}\n/yardim komut listesini gösterir")

    # ---- signals ------------------------------------------------------------------------------
    def publish_signal(self, res: Dict[str, Any]) -> None:
        cfg = self.e.cfg
        if self.tg is None or cfg.telegram_signals == "off" or not res.get("setup") or not res.get("playbook"):
            return
        approved = res.get("status") in APPROVED_STATES
        if not (approved or (cfg.telegram_signals == "setups" and res.get("analysis_valid"))):
            return
        if not grade_ok(res.get("grade") or "", cfg.telegram_min_grade):
            return
        try:
            side = res["setup"]["side"]
            key = (res["symbol"], res["style"], side)
            now = now_ms()
            if now - self._sent.get(key, 0) < STYLES[res["style"]].cooldown_minutes * 60_000:
                return                                  # one message per (symbol, style, side) per cooldown window
            self._sent[key] = now
            if not (res.get("mtf") or {}).get("tfs"):          # every signal carries the 8-timeframe picture
                res = {**res, "mtf": mtf_view(self.e.candles, res["symbol"], TIMEFRAMES)}
            self.tg.send(tgm.format_signal(res, cfg.describe_mode()))
        except Exception as exc:                        # notification problems must never reach the trading path
            self.e._throttled_error("telegram-signal", f"telegram signal failed: {type(exc).__name__}: {exc}")

    def publish_scan(self, res: Dict[str, Any]) -> None:
        """Radar tier-2 setup: information only (the symbol is outside the execution universe)."""
        cfg = self.e.cfg
        if self.tg is None or not cfg.telegram_radar or cfg.telegram_signals == "off" or not res.get("setup"):
            return
        if not grade_ok(res.get("grade") or "", cfg.telegram_min_grade):
            return
        key = ("scan", res["symbol"], res["setup"]["side"])
        now = now_ms()
        if now - self._sent.get(key, 0) < 4 * 3_600_000:
            return
        self._sent[key] = now
        try:
            self.tg.send(tgm.format_signal(res, cfg.describe_mode()))
        except Exception as exc:
            self.e._throttled_error("telegram-scan", f"telegram scan failed: {type(exc).__name__}: {exc}")

    # ---- events / digest -----------------------------------------------------------------------
    def flush_events(self) -> None:
        """Forward new CRITICAL events and trade events (persisted cursor: nothing lost or repeated across restarts)."""
        db, cfg = self.e.db, self.e.cfg
        last_id = int(db.kv_get("tg.last_event_id", 0) or 0)
        rows = db.query("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100", (last_id,))
        if not rows:
            return
        for r in rows:
            if r["level"] == "CRITICAL" or (cfg.telegram_trades and r["kind"] in EVENT_KINDS):
                self.tg.send(tgm.format_event(r["level"], r["message"]))
        db.kv_set("tg.last_event_id", rows[-1]["id"])

    def mtf_all(self) -> Dict[str, Dict[str, Any]]:
        return {s: mtf_view(self.e.candles, s, TIMEFRAMES) for s in self.e.cfg.symbols}

    def digest(self, force: bool = False) -> bool:
        db = self.e.db
        last = int(db.kv_get("tg.last_digest", 0) or 0)
        if not force and now_ms() - last < self.e.cfg.telegram_digest_hours * 3_600_000:
            return False
        db.kv_set("tg.last_digest", now_ms())
        self.tg.send(tgm.format_digest(self.e.snapshot(), self.mtf_all(), self.e.cfg.describe_mode()))
        return True

    # ---- commands ------------------------------------------------------------------------------
    def handle_command(self, cmd: str, arg: str) -> str:
        """Telegram command -> HTML reply. Read-only except /durdur (halt), which can only make the system safer."""
        e = self.e
        if cmd in ("/yardim", "/help", "/start"):
            return tgm.HELP
        if cmd in ("/durum", "/status"):
            st = e.status()
            eq, rk, ws = st.get("equity") or {}, st.get("risk") or {}, st.get("streams") or {}
            return "\n".join([
                f"<b>IMPERION v{tgm.esc(VERSION)}</b> · {tgm.esc(st['mode'])}",
                f"İşlem: {'etkin ✅' if st['trading_enabled'] else 'kapalı ⛔ ' + tgm.esc(st.get('why_not') or '')}",
                f"Equity: {tgm.num(eq.get('equity'))} · kullanılabilir: {tgm.num(eq.get('available'))}",
                f"Durdurma: {tgm.esc(rk.get('halted') or 'yok')} · ardışık kayıp: {tgm.esc(rk.get('consecutive_losses', 0))}",
                f"Açık pozisyon: {len(st.get('open_trades') or [])} · WebSocket: {'bağlı' if ws.get('market_connected') and ws.get('public_connected') else 'KOPUK'}",
            ] + [f"• {tgm.esc(t.get('side'))} {tgm.esc(t.get('symbol'))} giriş {tgm.num(t.get('entry'))} stop {tgm.num(t.get('stop'))}"
                 for t in (st.get("open_trades") or [])[:8]])
        if cmd in ("/analiz", "/analysis"):
            parts = arg.split()
            sym = (parts[0] if parts else "").upper().replace("/", "")
            if sym and not sym.endswith("USDT"):
                sym += "USDT"
            style = (parts[1].lower() if len(parts) > 1 else "") or ("day" if "day" in e.cfg.active_styles else e.cfg.active_styles[0])
            if sym not in e.cfg.symbols:
                return "Sembol bulunamadı. Örnek: /analiz SOLUSDT day\nİzlenenler: " + tgm.esc(", ".join(s.replace("USDT", "") for s in e.cfg.symbols))
            if style not in e.cfg.active_styles:
                return "Stil: " + tgm.esc(", ".join(e.cfg.active_styles))
            an = e.director.analyze(sym, style)                    # fresh read-only analysis, no shadow, no order
            return tgm.format_analysis(sym, e.snapshot()["symbols"][sym], an.to_dict(), mtf_view(e.candles, sym, TIMEFRAMES))
        if cmd in ("/piyasa", "/market"):
            return tgm.format_digest(e.snapshot(), self.mtf_all(), e.cfg.describe_mode())
        if cmd in ("/sinyaller", "/signals"):
            rows = e.db.query("SELECT * FROM decisions WHERE approved=1 AND ts>=? ORDER BY ts DESC LIMIT 12", (now_ms() - 86_400_000,))
            if not rows:
                return "Son 24 saatte onaylanan sinyal yok."
            out = ["<b>Son 24 saatin onaylı sinyalleri</b>"]
            for r in rows:
                s = json.loads(r["setup_json"] or "null") or {}
                out.append(f"{iso(r['ts'])[5:16]} {tgm.esc(r['symbol'])} {tgm.esc(r['direction'])} {tgm.esc(r['style'])} · "
                           f"{tgm.esc(s.get('grade') or '-')} {tgm.esc(s.get('playbook') or '')} · giriş {tgm.num(s.get('entry'))} "
                           f"stop {tgm.num(s.get('stop'))} · {tgm.esc(r['status'])}")
            return "\n".join(out)
        if cmd in ("/performans", "/performance"):
            perf = e.learning.summary().get("performance") or {}
            out = ["<b>Performans sınıfları</b> (ayrı ölçülür, birleştirilmez)"]
            for cls, label in (("shadow", "Gölge"), ("dry", "Dry"), ("testnet", "Testnet"), ("live", "Canlı")):
                st = perf.get(cls) or {}
                wr = st.get("win_rate")
                out.append(f"{label}: n={st.get('samples', 0)} · kazanma {tgm.num(wr * 100 if wr is not None else None, 3)}% · "
                           f"beklenti {tgm.num(st.get('expectancy_r'), 3)}R" + (f" · PnL {tgm.num(st.get('pnl_usdt'), 4)} USDT" if 'pnl_usdt' in st else ""))
            return "\n".join(out)
        if cmd in ("/performans", "/performance"):
            perf = e.learning.summary().get("performance") or {}
            out = ["<b>Performans sınıfları</b> (ayrı ölçülür, birleştirilmez)"]
            for cls, label in (("shadow", "Gölge"), ("dry", "Dry"), ("testnet", "Testnet"), ("live", "Canlı")):
                st = perf.get(cls) or {}
                wr = st.get("win_rate")
                out.append(f"{label}: n={st.get('samples', 0)} · kazanma {tgm.num(wr * 100 if wr is not None else None, 3)}% · "
                           f"beklenti {tgm.num(st.get('expectancy_r'), 3)}R" + (f" · PnL {tgm.num(st.get('pnl_usdt'), 4)} USDT" if "pnl_usdt" in st else ""))
            return "\n".join(out)
        if cmd in ("/kapi", "/gate"):
            g = e.live_gate()
            if not g["applies"]:
                return f"Canlı kapısı bu modda uygulanmıyor ({tgm.esc(e.cfg.mode)})."
            return ("✅ Canlı kapısı açık" if g["ok"] else "⛔ Canlı kapısı kapalı") + "\n" + "\n".join(f"• {tgm.esc(r)}" for r in g["reasons"])
        if cmd in ("/durdur", "/halt"):
            e.halt(f"telegram: {arg or 'manual halt'}")
            return "⛔ Yeni girişler durduruldu. Açık pozisyonların borsa tarafındaki stopları yerinde. Devam ettirmek için: python -m imperion resume"
        return "Bilinmeyen komut. /yardim"

    def poll_once(self) -> None:
        """One iteration of the Telegram worker: answer commands, then drain the outbound queue."""
        if self.tg is None:
            return
        if self.e.cfg.telegram_commands and self.e._due("tg_poll", 3):
            for cmd, arg in self.tg.poll_commands():
                try:
                    self.tg.send(self.handle_command(cmd, arg))
                except Exception as exc:
                    self.tg.send(f"Komut işlenemedi: {tgm.esc(type(exc).__name__)}")
                    self.e._throttled_error("telegram-command", f"telegram command {cmd} failed: {type(exc).__name__}: {exc}")
        self.tg.flush()

    def loop(self) -> None:
        while not self.e._stop.wait(1.0):
            self.poll_once()
