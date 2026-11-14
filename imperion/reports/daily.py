"""Daily report (e-mail): performance, trades, blocked signals, symbol snapshot, whale radar, AI and health."""
from __future__ import annotations

import html
import json
import smtplib
import ssl
from collections import Counter
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

from ..config import VERSION, Settings
from ..db import Database
from ..util import get_tz, iso, now_ms

DAY_MS = 86_400_000


# ---- scheduling -------------------------------------------------------------
def local_now(cfg: Settings, now: Optional[int] = None) -> datetime:
    return datetime.fromtimestamp((now if now is not None else now_ms()) / 1000, tz=get_tz(cfg.report_tz))


def report_due(cfg: Settings, db: Database, now: Optional[int] = None) -> Optional[str]:
    """Returns the local date string to report for, if a report is due (today's slot passed, not yet sent)."""
    ln = local_now(cfg, now)
    slot = ln.replace(hour=cfg.report_hour, minute=0, second=0, microsecond=0)
    if ln < slot or ln - slot > timedelta(hours=12):
        return None
    day = ln.strftime("%Y-%m-%d")
    return None if db.report_done(day) else day


def next_report_time(cfg: Settings, now: Optional[int] = None) -> datetime:
    ln = local_now(cfg, now)
    slot = ln.replace(hour=cfg.report_hour, minute=0, second=0, microsecond=0)
    return slot if ln < slot else slot + timedelta(days=1)


# ---- content ----------------------------------------------------------------
def _fmt(x: Optional[float], nd: int = 2, sign: bool = False) -> str:
    if x is None:
        return "-"
    return f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"


def gather(cfg: Settings, db: Database, snapshot: Dict[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    now = now if now is not None else now_ms()
    since = now - DAY_MS
    eq = db.equity_since(since)
    closed = db.closed_trades_since(since)
    opened = db.trades_opened_since(since)
    decisions = db.decisions_since(since)
    reasons: Counter = Counter()
    for d in decisions:
        if d["approved"]:
            continue
        try:
            for r in json.loads(d["reasons_json"] or "[]"):
                key = str(r).split(" ")[0].split(":")[0].split("(")[0]
                if key and not key.startswith("Directional"):
                    reasons[key] += 1
        except ValueError:
            pass
    events = db.events_since(since, ("WARNING", "ERROR", "CRITICAL"))
    pnl = sum(float(t["pnl_usdt"] or 0) for t in closed)
    wins = [t for t in closed if (t["pnl_usdt"] or 0) > 0]
    r_list = [float(t["r_multiple"] or 0) for t in closed]
    return {
        "generated": iso(now), "version": VERSION, "mode": cfg.describe_mode(),
        "equity_start": eq[0]["equity"] if eq else None, "equity_end": eq[-1]["equity"] if eq else None,
        "pnl_usdt": pnl, "closed": closed, "opened": opened, "open_now": db.open_trades(),
        "win_rate": (len(wins) / len(closed)) if closed else None, "avg_r": (sum(r_list) / len(r_list)) if r_list else None,
        "fees": sum(float(t["fees_usdt"] or 0) for t in closed),
        "decisions": len(decisions), "approved": sum(1 for d in decisions if d["approved"]),
        "block_reasons": reasons.most_common(8), "events": events[-12:], "event_counts": Counter(e["level"] for e in events),
        "symbols": snapshot.get("symbols", {}), "radar": snapshot.get("radar", []), "ai": snapshot.get("ai", {}),
        "risk": snapshot.get("risk", {}), "health": snapshot.get("health", {}), "market": snapshot.get("market", {}),
    }


def render(data: Dict[str, Any]) -> Tuple[str, str, str]:
    """Returns (subject, text, html)."""
    e0, e1 = data["equity_start"], data["equity_end"]
    pct = ((e1 - e0) / e0 * 100) if e0 and e1 and e0 > 0 else None
    subject = f"IMPERION günlük rapor | {data['generated'][:10]} | PnL {_fmt(data['pnl_usdt'], 2, True)} USDT | {len(data['closed'])} işlem"

    L: List[str] = [f"IMPERION FINANCE v{data['version']} - Günlük Rapor ({data['generated']})", f"Mod: {data['mode']}", ""]
    L += ["ÖZET (son 24 saat)",
          f"  Equity: {_fmt(e0)} -> {_fmt(e1)} USDT ({_fmt(pct, 2, True)}%)",
          f"  Kapanan işlem: {len(data['closed'])} | Net PnL: {_fmt(data['pnl_usdt'], 3, True)} USDT | Ücret: {_fmt(data['fees'], 3)} USDT",
          f"  Kazanma oranı: {_fmt(data['win_rate'] * 100 if data['win_rate'] is not None else None, 0)}% | Ort. R: {_fmt(data['avg_r'], 2, True)}",
          f"  Açılan: {len(data['opened'])} | Şu an açık: {len(data['open_now'])}",
          f"  Karar sayısı: {data['decisions']} | Onaylanan: {data['approved']}", ""]
    if data["closed"]:
        L.append("KAPANAN İŞLEMLER")
        for t in data["closed"]:
            L.append(f"  {t['side']:5} {t['symbol']:9} {t['style']:5} giriş {t['entry']:.6g} çıkış {t['exit_price'] or 0:.6g}  {_fmt(t['pnl_usdt'], 3, True)} USDT ({_fmt(t['r_multiple'], 2, True)}R) {t['exit_reason']}")
        L.append("")
    if data["open_now"]:
        L.append("AÇIK POZİSYONLAR")
        for t in data["open_now"]:
            L.append(f"  {t['side']:5} {t['symbol']:9} {t['style']:5} giriş {t['entry']:.6g} stop {t['stop']:.6g} hedef {t['tp2']:.6g}")
        L.append("")
    mk = data.get("market") or {}
    if mk:
        L += ["PİYASA GENEL GÖRÜNÜMÜ",
              f"  Seans: {mk.get('session', '-')} | BTC eğilimi: {mk.get('leader_bias', '-')} | genişlik (EMA50 üstü): "
              f"{_fmt(mk['breadth'] * 100 if mk.get('breadth') is not None else None, 0)}% ({mk.get('breadth_direction', '-')})", ""]
    L.append("SEMBOL DURUMU")
    for sym, s in data["symbols"].items():
        L.append(f"  {sym:9} fiyat {s.get('price', '-')} | MTF {s.get('mtf', '-')} | rejim {s.get('regime', '-')} | son karar {s.get('direction', '-')} {s.get('grade', '')} {s.get('playbook') or ''} | balina 15dk {_fmt(s.get('whale_net_15m'), 0, True)} USDT | defter {_fmt(s.get('book_imbalance'), 2, True)} | funding {_fmt(s.get('funding_pct'), 3, True)}%")
    L.append("")
    if data["radar"]:
        L.append("TARAMA LİSTESİ (radar ön tarama; A+/A derin analiz ve kabul sonrası işlem adayı)")
        for r in data["radar"]:
            L.append(f"  {r['symbol']:12} 24s hacim {_fmt(r.get('quote_volume', 0) / 1e6, 0)}M USDT | 1s {r.get('bias_1h', '-')} | balina 15dk {_fmt(r.get('whale_net_15m'), 0, True)} USDT")
        L.append("")
    if data["block_reasons"]:
        L.append("ENGELLENEN SİNYALLERİN NEDENLERİ")
        for k, v in data["block_reasons"]:
            L.append(f"  {k}: {v}")
        L.append("")
    ai = data["ai"] or {}
    L += ["YAPAY ZEKA (gölge öğrenme)",
          f"  Aktif ağırlık sürümü: v{ai.get('active_version', 0)} | çözülmüş gölge işlem: {ai.get('resolved_shadows', 0)} | açık: {ai.get('open_shadows', 0)}",
          f"  Gölge beklenti: {_fmt(ai.get('shadow_expectancy_r'), 2, True)}R | kazanma: {_fmt((ai.get('shadow_win_rate') or 0) * 100 if ai.get('shadow_win_rate') is not None else None, 0)}%",
          f"  Onay bekleyen öneri: {len(ai.get('pending_proposals', []))}",
          "  Kazanma olasılığı modeli: " + ("yok (yeterli veri bekleniyor)" if not ai.get("win_model") else
                                             ("doğrulandı" if ai["win_model"].get("validated") else "yalnızca danışma") +
                                             f" (AUC {(ai['win_model'].get('metrics') or {}).get('auc', '-')}, Brier {(ai['win_model'].get('metrics') or {}).get('brier', '-')})"), ""]
    autonomous = ai.get("autonomous") or {}
    if autonomous:
        mind = autonomous.get("mind") or {}
        L.append("AUTONOMOUS PRO (yalnızca gölge araştırma; risk/oy etkisi yok)")
        L.append(f"  Çözülmüş deneyim: {autonomous.get('experience_count', 0)} | beklenti: {_fmt(mind.get('expectancy_r'), 3, True)}R")
        L.append("  Otonom ağırlık/risk değiştirme: KAPALI | manuel onay zorunlu")
        L.append("")
    perf = (ai.get("performance") or {})
    if perf:
        L.append("PERFORMANS SINIFLARI (ayrı ölçülür, asla birleştirilmez)")
        for cls, label in (("shadow", "Gölge (simülasyon)"), ("dry", "Dry (kâğıt)"), ("testnet", "Testnet"), ("live", "Canlı (gerçek para)")):
            st = perf.get(cls) or {}
            L.append(f"  {label:22} n={st.get('samples', 0):<4} kazanma {_fmt((st['win_rate'] or 0) * 100 if st.get('win_rate') is not None else None, 0)}% | "
                     f"beklenti {_fmt(st.get('expectancy_r'), 2, True)}R | toplam {_fmt(st.get('total_r'), 2, True)}R"
                     + (f" | PnL {_fmt(st.get('pnl_usdt'), 2, True)} USDT" if "pnl_usdt" in st else ""))
        L.append("")
    rk = data["risk"] or {}
    L += ["RİSK DURUMU", f"  Durdurma: {rk.get('halted') or 'yok'} | günlük yeni risk: {rk.get('daily_new_risk_pct', 0)}% | ardışık kayıp: {rk.get('consecutive_losses', 0)}", ""]
    h = data["health"] or {}
    ws = h.get("ws") or {}
    ws_txt = "-" if not ws else ("bağlı" if ws.get("market_connected") and ws.get("public_connected") else "KOPUK") + f" (yeniden bağlanma: {ws.get('reconnects', 0)})"
    L += ["SİSTEM SAĞLIĞI", f"  WebSocket: {ws_txt} | işlem etkin: {'evet' if h.get('trading_enabled') else 'hayır'} | uyarı/hata sayısı: {dict(data['event_counts']) or 0}"]
    for ev in data["events"]:
        L.append(f"   - [{ev['level']}] {iso(ev['ts'])[11:19]} {ev['message'][:140]}")
    L += ["", "Not: Bu rapor kâr garantisi vermez; sonuçlar küçük örneklem içindir."]
    text = "\n".join(L)

    esc = html.escape
    rows = "".join(
        f"<tr><td>{esc(sym)}</td><td>{esc(str(s.get('price', '-')))}</td><td>{esc(str(s.get('mtf', '-')))}</td><td>{esc(str(s.get('regime', '-')))}</td>"
        f"<td>{esc(str(s.get('direction', '-')))}</td><td>{esc(str(s.get('grade', '-')))}</td><td>{_fmt(s.get('whale_net_15m'), 0, True)}</td><td>{_fmt(s.get('book_imbalance'), 2, True)}</td></tr>"
        for sym, s in data["symbols"].items())
    trade_rows = "".join(
        f"<tr><td>{esc(t['side'])}</td><td>{esc(t['symbol'])}</td><td>{esc(t['style'])}</td><td>{_fmt(t['pnl_usdt'], 3, True)}</td><td>{_fmt(t['r_multiple'], 2, True)}</td><td>{esc(str(t['exit_reason']))}</td></tr>"
        for t in data["closed"]) or "<tr><td colspan=6>Kapanan işlem yok</td></tr>"
    block = "".join(f"<li>{esc(k)}: {v}</li>" for k, v in data["block_reasons"]) or "<li>-</li>"
    html_doc = f"""<html><body style="font-family:Arial,sans-serif;color:#1b2430;max-width:860px">
<h2 style="margin-bottom:2px">IMPERION FINANCE v{esc(data['version'])} — Günlük Rapor</h2>
<div style="color:#667">{esc(data['generated'])} · {esc(data['mode'])}</div>
<h3>Özet (24 saat)</h3>
<table cellpadding="5" style="border-collapse:collapse"><tr><td>Equity</td><td><b>{_fmt(e0)} → {_fmt(e1)} USDT ({_fmt(pct, 2, True)}%)</b></td></tr>
<tr><td>Net PnL</td><td><b>{_fmt(data['pnl_usdt'], 3, True)} USDT</b> (ücret {_fmt(data['fees'], 3)})</td></tr>
<tr><td>Kapanan / açılan / açık</td><td>{len(data['closed'])} / {len(data['opened'])} / {len(data['open_now'])}</td></tr>
<tr><td>Kazanma · Ort. R</td><td>{_fmt(data['win_rate'] * 100 if data['win_rate'] is not None else None, 0)}% · {_fmt(data['avg_r'], 2, True)}R</td></tr>
<tr><td>Karar / onaylanan</td><td>{data['decisions']} / {data['approved']}</td></tr></table>
<h3>Kapanan işlemler</h3>
<table border="1" cellpadding="4" style="border-collapse:collapse;border-color:#ccd"><tr><th>Yön</th><th>Sembol</th><th>Stil</th><th>PnL</th><th>R</th><th>Çıkış</th></tr>{trade_rows}</table>
<h3>Sembol durumu</h3>
<table border="1" cellpadding="4" style="border-collapse:collapse;border-color:#ccd"><tr><th>Sembol</th><th>Fiyat</th><th>MTF</th><th>Rejim</th><th>Karar</th><th>Derece</th><th>Balina 15dk</th><th>Defter</th></tr>{rows}</table>
<h3>Engellenen sinyal nedenleri</h3><ul>{block}</ul>
<h3>Yapay zeka (gölge)</h3><div>Aktif sürüm v{(data['ai'] or {}).get('active_version', 0)} · çözülmüş gölge işlem {(data['ai'] or {}).get('resolved_shadows', 0)} · onay bekleyen öneri {len((data['ai'] or {}).get('pending_proposals', []))}</div>
<h3>Risk</h3><div>Durdurma: {esc(str((data['risk'] or {}).get('halted') or 'yok'))}</div>
<p style="color:#889;font-size:12px">Bu rapor kâr garantisi vermez; sonuçlar küçük örneklem içindir.</p></body></html>"""
    return subject, text, html_doc


# ---- mail -------------------------------------------------------------------
def send_mail(cfg: Settings, subject: str, text: str, html_body: Optional[str] = None) -> Tuple[bool, str]:
    if not (cfg.smtp_host and cfg.mail_to and cfg.mail_from):
        return False, "SMTP_HOST / MAIL_FROM / MAIL_TO not configured"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg.mail_from, cfg.mail_to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        ctx = ssl.create_default_context()
        if cfg.smtp_ssl or cfg.smtp_port == 465:
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=ctx) as s:
                if cfg.smtp_user:
                    s.login(cfg.smtp_user, cfg.smtp_pass)
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
                s.starttls(context=ctx)
                if cfg.smtp_user:
                    s.login(cfg.smtp_user, cfg.smtp_pass)
                s.send_message(msg)
        return True, "sent"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
