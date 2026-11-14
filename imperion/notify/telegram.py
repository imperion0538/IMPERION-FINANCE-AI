"""Telegram delivery: signals, 8-timeframe analysis, trade events and critical alerts.

Design rules
  * Sending never blocks or breaks trading: messages go through a bounded queue drained by one worker, every error
    is swallowed and counted.
  * Telegram limits are respected (>= 1.1 s between messages, HTTP 429 retry_after honoured, 4096-char split).
  * Inbound commands are accepted ONLY from the configured chat and are read-only, plus /durdur (halt), which can
    only make the system safer. Nothing that opens, resumes or flattens can be triggered from Telegram.
  * The bot token is never logged or echoed.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

log = logging.getLogger("imperion.telegram")

API = "https://api.telegram.org"
MAX_LEN = 4000
QUEUE_MAX = 200
TF_ORDER = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
ARROW = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪", "UNKNOWN": "▫️"}
STYLE_TR = {"scalp": "SCALP", "day": "DAY TRADE", "swing": "SWING"}

Transport = Callable[[str, Dict[str, Any], float], Tuple[int, Dict[str, Any]]]


def _default_transport(url: str, payload: Dict[str, Any], timeout: float) -> Tuple[int, Dict[str, Any]]:
    import requests

    resp = requests.post(url, json=payload, timeout=timeout)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def num(x: Any, digits: int = 6) -> str:
    try:
        return f"{float(x):.{digits}g}"
    except (TypeError, ValueError):
        return "-"


def split_message(text: str, limit: int = MAX_LEN) -> List[str]:
    """Split on line boundaries so no HTML tag is cut in half (messages are built one tag per line)."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur); cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


class Notifier:
    def __init__(self, token: str, chat_id: str, transport: Optional[Transport] = None, min_interval: float = 1.1, timeout: float = 10.0):
        self._token, self.chat_id = token, str(chat_id)
        self.transport = transport or _default_transport
        self.min_interval, self.timeout = min_interval, timeout
        self._q: Deque[str] = deque()
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._pause_until = 0.0
        self._offset = 0
        self.sent = self.failed = self.dropped = 0
        self.last_error = ""

    # ---- outbound --------------------------------------------------------
    def send(self, text: str) -> None:
        """Queue a message (HTML). Oldest messages are dropped if the queue is full."""
        with self._lock:
            for part in split_message(text):
                if len(self._q) >= QUEUE_MAX:
                    self._q.popleft(); self.dropped += 1
                self._q.append(part)

    def pending(self) -> int:
        with self._lock:
            return len(self._q)

    def _call(self, method: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        return self.transport(f"{API}/bot{self._token}/{method}", payload, self.timeout)

    def flush(self, max_messages: int = 20) -> int:
        """Send queued messages respecting the rate limit. Returns how many were delivered. Never raises."""
        done = 0
        while done < max_messages:
            now = time.time()
            if now < self._pause_until:
                break
            with self._lock:
                if not self._q:
                    break
                text = self._q[0]
            wait = self.min_interval - (now - self._last_send)
            if wait > 0:
                time.sleep(wait)
            try:
                status, body = self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            except Exception as exc:
                self.failed += 1
                self.last_error = type(exc).__name__          # the exception text may contain the URL (token): keep the type only
                self._pause_until = time.time() + 15
                break
            self._last_send = time.time()
            if status == 429:
                retry = float(((body or {}).get("parameters") or {}).get("retry_after", 5) or 5)
                self._pause_until = time.time() + min(max(retry, 1.0), 120.0)
                break
            with self._lock:
                if self._q and self._q[0] is text:
                    self._q.popleft()
            if status == 200 and (body or {}).get("ok", True):
                self.sent += 1; done += 1
            else:                                              # bad request (e.g. malformed HTML): drop it, do not retry forever
                self.failed += 1
                self.last_error = f"HTTP {status}: {str((body or {}).get('description', ''))[:120]}"
        return done

    # ---- inbound ---------------------------------------------------------
    def poll_commands(self) -> List[Tuple[str, str]]:
        """New (command, argument) pairs sent from the configured chat. Messages from any other chat are ignored."""
        out: List[Tuple[str, str]] = []
        try:
            status, body = self._call("getUpdates", {"offset": self._offset, "timeout": 0, "allowed_updates": ["message"]})
        except Exception as exc:
            self.last_error = type(exc).__name__
            return out
        if status != 200 or not isinstance(body, dict):
            return out
        for upd in body.get("result") or []:
            try:
                self._offset = max(self._offset, int(upd["update_id"]) + 1)
                msg = upd.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                    continue
                text = str(msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                cmd, _, arg = text.partition(" ")
                out.append((cmd.split("@")[0].lower(), arg.strip()[:200]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def status(self) -> Dict[str, Any]:
        return {"sent": self.sent, "failed": self.failed, "dropped": self.dropped, "pending": self.pending(), "last_error": self.last_error}


# =============================================================================== message builders
PB_TR = {"sweep_reversal": "Likidite süpürmesi + dönüş", "trend_pullback": "Trend geri çekilmesi", "breakout_retest": "Kırılım + yeniden test",
         "range_fade": "Yatay aralık kenar dönüşü"}
AGENT_TR = {"trend": "Trend (üst TF)", "structure": "Yapı", "liquidity": "Likidite", "zones": "Bölgeler", "trigger": "Tetik",
            "orderflow": "Emir akışı", "microstructure": "Mikro yapı", "momentum": "Momentum", "context": "Piyasa bağlamı",
            "derivatives": "Fonlama", "external": "TradingView"}
GRADE_ICON = {"A+": "🏆", "A": "🥇", "B": "🥈", "C": "▫️"}
_CODE = re.compile(r"^[A-Z][A-Z0-9]*_[A-Z0-9_]+")       # machine reason codes such as MAX_POSITIONS (...)


def mtf_line(tfs: Dict[str, Dict[str, Any]]) -> str:
    return " ".join(f"{tf}{ARROW.get((tfs.get(tf) or {}).get('bias', 'UNKNOWN'), '▫️')}" for tf in TF_ORDER)


def _agent_line(name: str, a: Dict[str, Any], side: Optional[str]) -> str:
    want = "BULLISH" if side == "LONG" else "BEARISH" if side == "SHORT" else None
    if not a.get("ok"):
        mark = "▫️"
    elif a.get("d") == want:
        mark = "✅"
    elif a.get("d") in ("BULLISH", "BEARISH"):
        mark = "❌"
    else:
        mark = "⚪"
    conf = f" {a.get('c', 0) * 100:.0f}%" if a.get("ok") and a.get("d") in ("BULLISH", "BEARISH") else ""
    return f"{mark} {esc(AGENT_TR.get(name, name))}{conf}: {esc(str(a.get('summary', ''))[:90])}"


def format_signal(res: Dict[str, Any], mode: str) -> str:
    s, pb = res.get("setup") or {}, res.get("playbook") or {}
    side = res.get("direction") if res.get("direction") in ("LONG", "SHORT") else pb.get("side", "-")
    icon = "🟢" if side == "LONG" else "🔴"
    status = res.get("status", "")
    tag = {"TRADED": "İŞLEM AÇILDI", "APPROVED": "ONAYLANDI", "APPROVED_NOT_TRADED": "SİNYAL (emir gönderilmedi)",
           "ENTRY_FAILED": "ONAYLANDI (giriş başarısız)",
           "SCAN_ONLY": "RADAR ÖN ANALİZİ — bu adım emir göndermez"}.get(status, "KURULUM (risk / AI engeli)")
    mk, ai, grade = res.get("market") or {}, res.get("ai") or {}, res.get("grade") or "-"
    lines = [
        f"{icon} <b>{esc(res.get('symbol'))} {esc(side)}</b> · {esc(STYLE_TR.get(res.get('style'), res.get('style')))} · "
        f"{GRADE_ICON.get(grade, '')} <b>{esc(grade)}</b>",
        f"<i>{esc(tag)}</i>",
        f"📘 {esc(PB_TR.get(pb.get('name'), pb.get('name') or '-'))}",
        "",
        f"Giriş: <code>{num(s.get('entry'))}</code>",
        f"Stop: <code>{num(s.get('stop'))}</code> ({num(s.get('stop_pct'), 3)}%)",
        f"TP1: <code>{num(s.get('tp1'))}</code>",
        f"TP2: <code>{num(s.get('tp2'))}</code> (RR {num(s.get('rr'), 3)})",
        "",
        f"Kalite: <b>{num(res.get('quality'), 3)}</b>/100 · Uzlaşı: {num(res.get('net'), 2)} ({len(res.get('agree') or [])} ajan)",
        f"Rejim: {esc(mk.get('regime') or '-')} ({esc(mk.get('profile') or '-')}) · Seans: {esc(mk.get('session') or '-')} · "
        f"Volatilite: {esc(mk.get('volatility') or '-')}",
    ]
    if ai.get("p_win") is not None:
        lines.append(f"AI kazanma olasılığı: {num(float(ai['p_win']) * 100, 3)}% ({'doğrulanmış model' if ai.get('model_validated') else 'danışma'})")
    steps = pb.get("steps") or []
    if steps:
        lines += ["", "<b>Kurulum</b>"] + [f"• {esc(x)}" for x in steps[:6]]
    agents = res.get("agents") or {}
    if agents:
        lines += ["", "<b>Ajanlar</b>"] + [_agent_line(k, v, side) for k, v in agents.items() if v.get("ok")][:11]
    tfs = (res.get("mtf") or {}).get("tfs") or {}
    if tfs:
        mtf = res["mtf"]
        lines += ["", f"8 zaman dilimi: {mtf_line(tfs)}", f"MTF uyumu: {num((mtf.get('confluence') or 0) * 100, 3)}% {esc(mtf.get('direction', '-'))}"]
    blockers = [] if status in ("TRADED", "APPROVED") else [r for r in (res.get("reasons") or []) if _CODE.match(str(r))][-3:]
    if blockers:
        lines += ["", "Not: " + esc("; ".join(str(b) for b in blockers))]
    lines += ["", f"<i>{esc(mode)} · yatırım tavsiyesi değildir</i>"]
    return "\n".join(lines)


def format_digest(snapshot: Dict[str, Any], mtf_by_symbol: Dict[str, Dict[str, Any]], mode: str) -> str:
    mk, risk = snapshot.get("market") or {}, snapshot.get("risk") or {}
    breadth = mk.get("breadth")
    lines = ["📊 <b>IMPERION piyasa analizi</b>",
             f"Seans: {esc(mk.get('session', '-'))} · BTC eğilimi: {esc(mk.get('leader_bias', '-'))} · Genişlik: "
             f"{num(breadth * 100, 3) + '%' if breadth is not None else '-'}",
             ""]
    for sym, s in (snapshot.get("symbols") or {}).items():
        mtf = mtf_by_symbol.get(sym) or {}
        last = f" · son: {esc(s.get('direction'))} {esc(s.get('grade'))}" if s.get("direction") not in (None, "-", "NO TRADE") else ""
        lines.append(f"<b>{esc(sym.replace('USDT', ''))}</b> <code>{num(s.get('price'))}</code> {esc(s.get('regime', '-'))} · "
                     f"{num((mtf.get('confluence') or 0) * 100, 3)}% {esc(mtf.get('direction', '-'))}{last}")
        lines.append(mtf_line(mtf.get("tfs") or {}))
    radar = [r for r in (snapshot.get("radar") or []) if r.get("bias_1h") in ("BULLISH", "BEARISH")][:8]
    if radar:
        lines += ["", "<b>Radar ön tarama (1s; işlem kabulü ayrıca yapılır)</b>"]
        lines += [f"{ARROW.get(r['bias_1h'], '▫️')} {esc(r['symbol'].replace('USDT', ''))} {num(r.get('price_change_pct'), 3)}% / 24s" for r in radar]
    deep = snapshot.get("radar_deep") or {}
    if deep:
        lines += ["", "<b>Radar derin analiz (A+/A ayrıca onaylanır)</b>"]
        lines += [f"{esc(s.replace('USDT', ''))}: {esc(d.get('direction'))} {esc(d.get('grade') or '')} {esc(PB_TR.get(d.get('playbook'), d.get('playbook') or ''))}"
                  for s, d in deep.items()]
    lines += ["", f"Durdurma: {esc(risk.get('halted') or 'yok')} · günlük yeni risk: {esc(risk.get('daily_new_risk_pct', 0))}%",
              f"<i>{esc(mode)}</i>"]
    return "\n".join(lines)


def format_analysis(symbol: str, row: Dict[str, Any], an: Optional[Dict[str, Any]], mtf: Dict[str, Any]) -> str:
    """/analiz reply: market row + the latest Director analysis (agents, playbooks, price-action map) + 8 timeframes."""
    lines = [f"🔎 <b>{esc(symbol)} analizi</b>",
             f"Fiyat: <code>{num(row.get('price'))}</code> · Rejim: {esc(row.get('regime', '-'))} · MTF: {esc(row.get('mtf', '-'))}",
             f"Balina net (15 dk): {num(row.get('whale_net_15m'), 6)} USDT · Defter dengesi: {num(row.get('book_imbalance'), 3)} · "
             f"Fonlama: {num(row.get('funding_pct'), 3)}%", ""]
    if an:
        m = (an.get("maps") or {}).get("setup") or {}
        if m.get("ok"):
            lb = m.get("last_break") or {}
            lines.append(f"<b>Price action ({esc(an.get('style'))}, kurulum TF)</b>")
            lines.append(f"Yapı: {esc(m.get('trend'))} {esc(m.get('swing_bias'))}" + (f" · son {esc(lb.get('kind'))} {esc(lb.get('dir'))} ({lb.get('age')} mum)" if lb else ""))
            if m.get("range"):
                lines.append(f"Aralık: {num(m['range']['low'])}–{num(m['range']['high'])} (konum %{m['range']['position'] * 100:.0f})")
            if m.get("pools_above"):
                lines.append("Üst likidite: " + ", ".join(f"{num(p['price'])} {esc(p['kind'])}" for p in m["pools_above"][:2]))
            if m.get("pools_below"):
                lines.append("Alt likidite: " + ", ".join(f"{num(p['price'])} {esc(p['kind'])}" for p in m["pools_below"][:2]))
            if m.get("sweeps"):
                sw = m["sweeps"][0]
                lines.append(f"Süpürme: {esc(sw['kind'])} @ {num(sw['level'])} ({esc(sw['dir'])}, {sw['age']} mum önce)")
        pbs = an.get("playbooks") or []
        if pbs:
            lines.append("Kurulumlar: " + "; ".join(f"{esc(PB_TR.get(p['name'], p['name']))} {esc(p['side'])} {p['score']:.2f}" for p in pbs[:3]))
        lines.append(f"Karar: <b>{esc(an.get('direction'))}</b>" + (f" · {esc(an.get('grade'))}" if an.get("grade") else ""))
        for r in (an.get("reasons") or [])[:3]:
            lines.append(f"  · {esc(str(r)[:160])}")
        agents = an.get("agents") or {}
        if agents:
            lines += ["", "<b>Ajanlar</b>"] + [_agent_line(k, v, an.get("direction")) for k, v in agents.items() if v.get("ok")]
        autonomous = ((an.get("regime") or {}).get("autonomous") or {})
        extra = autonomous.get("agents") or {}
        if extra:
            lines += ["", "<b>AUTONOMOUS PRO — ek analiz (gölge, oy kullanmaz)</b>"]
            for k, v in extra.items():
                mark = ARROW.get(v.get("direction"), "▫️") if v.get("available") else "▫️"
                lines.append(f"{mark} {esc(k)}: {esc(str(v.get('reason') or '-')[:66])}")
            vp = m.get("volume_profile") or {}
            if vp:
                lines.append(f"VP tahmini: POC {num(vp.get('poc'))} · VAH {num(vp.get('vah'))} · VAL {num(vp.get('val'))}")
    lines += ["", "Zaman dilimleri (yapı / EMA trendi):"]
    for tf in TF_ORDER:
        v = (mtf.get("tfs") or {}).get(tf) or {}
        lines.append(f"{ARROW.get(v.get('bias', 'UNKNOWN'), '▫️')} <code>{tf:>3}</code> {esc(v.get('structure', '-'))} / {esc(v.get('trend', '-'))}")
    lines.append(f"MTF uyumu: {num((mtf.get('confluence') or 0) * 100, 3)}% {esc(mtf.get('direction', '-'))}")
    return "\n".join(lines)


def format_event(level: str, message: str) -> str:
    icon = "🚨" if level == "CRITICAL" else "⚠️" if level in ("WARNING", "ERROR") else "ℹ️"
    head = message.split(" ", 1)[0]
    if head == "OPENED":
        icon = "✅"
    elif head == "CLOSED":
        icon = "🏁"
    return f"{icon} {esc(message)}"


HELP = ("<b>IMPERION komutları</b>\n/durum – hesap, risk ve bağlantı durumu\n/analiz SEMBOL [stil] – müdür + ajan analizi, 8 zaman dilimi "
        "(ör. /analiz SOLUSDT day)\n/piyasa – tüm sembollerin özeti ve tarama listesi\n/sinyaller – son 24 saatin onaylı sinyalleri\n"
        "/performans – gölge / dry / testnet / canlı sonuçları ayrı ayrı\n/kapi – testnet → canlı kapısının durumu\n"
        "/durdur [neden] – yeni girişleri durdur\n/yardim – bu liste\n"
        "<i>Devam ettirme ve pozisyon kapatma güvenlik nedeniyle yalnızca yönetici API'si / CLI ile yapılır.</i>")
