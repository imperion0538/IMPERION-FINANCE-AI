"""Command line: `python -m imperion <command>`.

  run            start the engine (this is what the container runs)
  analyze        one-off Director analysis of a symbol from live public market data (no keys, no orders)
  backtest       replay history through the real Director: --symbol SOLUSDT --style day --days 14 [--step N] [--json]
  preflight      read-only exchange / API-key safety checks (never places an order)
  testnet-check  Binance Futures Demo validation (read-only by default; order smoke test only with explicit opt-in);
                 a passed run with the order lifecycle is stored as evidence for the live gate
  approve-live / revoke-live / gate-status   manual Testnet -> Live gate
  import-evidence FILE / verify-evidence FILE  signed (HMAC) testnet evidence from the GitHub workflow artifact
  status         risk state, open trades and recent events from the local database
  report         print today's report (add --send to deliver it now)
  halt/resume    stop / re-allow NEW entries (shared database; open positions keep their exchange stops)
  ai-status | ai-run | ai-approve N | ai-rollback
  selftest       run the execution-safety scenarios against an in-process fake exchange
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from typing import List, Optional

from .config import CORE_NAME, CORE_VERSION, STYLES, TIMEFRAMES, VERSION, load_settings
from .util import now_ms, setup_logging


def _engine(cfg):
    from .runtime.engine import Engine
    return Engine(cfg)


def cmd_run(args) -> int:
    cfg = load_settings()
    setup_logging(cfg.log_level)
    engine = _engine(cfg)

    def _sig(signum, _frame):
        engine._stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    engine.start()
    engine.run_forever()
    return 0


def analyze_symbol(cfg, symbol: str, styles: List[str], rest=None) -> List[dict]:
    """Fetch public candles for every timeframe and run the Director once per style. Read-only."""
    from .core.director import Director
    from .data.candles import CandleStore
    from .data.microstructure import BookStore, TradeStore
    from .data.universe import DerivativesStore
    from .exchange.rest import RestClient
    rest = rest or RestClient(cfg.rest_base, name="public")
    store, deriv = CandleStore(), DerivativesStore()
    for sym in dict.fromkeys([symbol, "BTCUSDT"]):
        for tf in TIMEFRAMES:
            store.ingest(sym, tf, rest.klines(sym, tf, limit=500))
    try:
        deriv.refresh(rest)
    except Exception:
        pass
    director = Director(cfg, store, TradeStore(cfg.whale_floor_usdt), BookStore(), deriv)
    return [director.analyze(symbol, st).to_dict() for st in styles]


def _print_analysis(a: dict) -> None:
    print(f"\n=== {a['symbol']} / {a['style']} — {a['direction']} {a['grade'] or ''} (quality {a['quality']})")
    reg = a.get("regime") or {}
    print(f"regime {reg.get('regime')} ({reg.get('profile')}), volatility {reg.get('volatility')}, session {reg.get('session')}")
    m = (a.get("maps") or {}).get("setup") or {}
    if m.get("ok"):
        print(f"structure {m['trend']} {m['swing_bias']} last break {m.get('last_break')} range {m.get('range')}")
    for p in a.get("playbooks") or []:
        print(f"  playbook {p['name']} {p['side']} score {p['score']}: " + " | ".join(p["steps"][:4]))
    for k, r in (a.get("agents") or {}).items():
        print(f"  {k:<14} {('-' if not r['ok'] else r['d']):<8} {r['c']:.2f}  {r['summary']}")
    for r in a.get("reasons") or []:
        print(f"  reason: {r}")
    if a.get("setup"):
        s = a["setup"]
        print(f"  plan: {s['side']} entry {s['entry']:.6g} stop {s['stop']:.6g} tp1 {s['tp1']:.6g} tp2 {s['tp2']:.6g} RR {s['rr']}")


def cmd_analyze(args) -> int:
    cfg = load_settings()
    setup_logging("WARNING")
    styles = [args.style] if args.style else list(cfg.active_styles)
    if any(s not in STYLES for s in styles):
        raise ValueError(f"--style must be one of {sorted(STYLES)}")
    res = analyze_symbol(cfg, args.symbol.upper(), styles)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        for a in res:
            _print_analysis(a)
    return 0


def cmd_backtest(args, rest=None) -> int:
    cfg = load_settings()
    setup_logging("WARNING")
    from .exchange.rest import RestClient
    from .research import backtest as bt
    symbol = args.symbol.upper()
    if args.style not in STYLES:
        raise ValueError(f"--style must be one of {sorted(STYLES)}")
    if args.days <= 0 or args.step <= 0:
        raise ValueError("--days and --step must be positive")
    rest = rest or RestClient(cfg.rest_base, name="public")
    end = now_ms()
    start = end - int(args.days * 86_400_000)
    print(f"downloading {symbol} history for the {args.style} style ({args.days} days) ...", file=sys.stderr)
    series = bt.download(rest, symbol, args.style, start, end)
    rep = bt.run_backtest(series, symbol, args.style, cfg, start_ms=start, step=args.step)
    print(json.dumps(rep, indent=2, default=str) if args.json else bt.format_report(rep))
    return 0


def cmd_sensitivity(args, rest=None) -> int:
    cfg = load_settings()
    setup_logging("WARNING")
    from .exchange.rest import RestClient
    from .research import backtest as bt
    from .research.sensitivity import format_sensitivity, run_sensitivity
    if args.style not in STYLES or args.days <= 0 or args.step <= 0:
        raise ValueError("invalid --style / --days / --step")
    rest = rest or RestClient(cfg.rest_base, name="public")
    end = now_ms()
    start = end - int(args.days * 86_400_000)
    series = bt.download(rest, args.symbol.upper(), args.style, start, end)
    rep = run_sensitivity(series, args.symbol.upper(), args.style, cfg, start_ms=start, step=args.step)
    print(json.dumps(rep, indent=2, default=str) if args.json else format_sensitivity(rep))
    return 0


def cmd_optimize(args, rest=None) -> int:
    cfg = load_settings()
    setup_logging("WARNING")
    from .exchange.rest import RestClient
    from .research import backtest as bt
    from .research.optuna_optimizer import optimize_optuna, optimize_by_regime
    if args.style not in STYLES or args.days <= 0:
        raise ValueError("invalid --style / --days")
    rest = rest or RestClient(cfg.rest_base, name="public")
    end = now_ms()
    start = end - int(args.days * 86_400_000)
    print(f"Optuna optimizing {args.symbol} / {args.style} ({args.trials} trials) ...", file=sys.stderr)
    series = bt.download(rest, args.symbol.upper(), args.style, start, end)
    if args.by_regime:
        result = optimize_by_regime(series, args.symbol.upper(), args.style, cfg, n_trials=args.trials)
    else:
        result = optimize_optuna(series, args.symbol.upper(), args.style, cfg, n_trials=args.trials)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_preflight(args) -> int:
    cfg = load_settings()
    setup_logging("WARNING")
    if not cfg.has_credentials:
        print("BINANCE_API_KEY / BINANCE_API_SECRET are not set: nothing to check.")
        return 2
    from .exchange.filters import parse_exchange_info
    from .exchange.rest import RestClient
    from .execution.binance import BinanceExecutor
    pub = RestClient(cfg.rest_base, name="public")
    trade = RestClient(cfg.trade_base, cfg.api_key, cfg.api_secret, guard=pub.guard, name="trade")
    spot = RestClient(cfg.spot_base, cfg.api_key, cfg.api_secret, guard=pub.guard, name="spot")
    res = BinanceExecutor(cfg, trade, spot, parse_exchange_info(pub.exchange_info())).preflight()
    print(json.dumps(res, indent=2, default=str))
    print("\nRESULT:", "OK - safe to start" if res["ok"] else "NOT OK - fix the errors above before trading")
    print("Live orders additionally need LIVE_TRADING_CONFIRM=I_ACCEPT_REAL_MONEY_RISK;", "set." if cfg.live_confirmed else "currently NOT set (observe-only).")
    return 0 if res["ok"] else 1


def cmd_testnet(args) -> int:
    from .testnet import main as testnet_main
    return testnet_main()


def cmd_gate(args) -> int:
    cfg = load_settings()
    from .db import Database
    from . import gate
    db = Database(cfg.db_path)
    if args.action == "approve":
        try:
            ap = gate.approve_live(db, args.operator, args.confirm, cfg.live_gate_max_age_days, cfg.testnet_evidence_secret)
        except ValueError as exc:
            print(f"NOT approved: {exc}", file=sys.stderr)
            return 1
        print(f"LIVE trading approved by {ap['operator']} (evidence {ap['evidence_hash'][:12]}, version {ap['version']})")
        return 0
    if args.action == "import":
        try:
            ev = gate.import_testnet_evidence(db, args.file, cfg.testnet_evidence_secret, cfg.live_gate_max_age_days)
        except (ValueError, OSError) as exc:
            print(f"NOT imported: {exc}", file=sys.stderr)
            return 1
        print(f"signed testnet evidence imported: {gate.evidence_hash(ev)} ({ev['symbol']}, {ev['version']}, run {ev['run_id'][:8]})")
        print("next: python -m imperion approve-live --operator NAME --confirm " + gate.APPROVAL_PHRASE)
        return 0
    if args.action == "revoke":
        gate.revoke_live(db, by="cli")
        print("Live approval revoked: no real-money order will be sent until approved again.")
        return 0
    st = gate.live_gate_status(db, cfg)
    print(json.dumps(st, indent=2, default=str))
    return 0 if st["ok"] else 1


def cmd_verify_evidence(args) -> int:
    """Check a signed evidence file without touching any database (used by the testnet / release workflows)."""
    cfg = load_settings()
    from . import gate
    try:
        ev = gate.read_evidence_file(args.file)
    except (ValueError, OSError) as exc:
        print(f"EVIDENCE=INVALID cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    problems = gate.verify_evidence(ev, cfg.testnet_evidence_secret, cfg.live_gate_max_age_days)
    for p in problems:
        print(f"FAIL {p}")
    digest = gate.evidence_hash(ev)
    if args.expect_sha256 and args.expect_sha256 != digest:
        print(f"FAIL EVIDENCE_HASH_MISMATCH (expected {args.expect_sha256}, file {digest})")
        problems.append("hash")
    print(f"EVIDENCE_SHA256={digest}")
    print("EVIDENCE=" + ("VALID" if not problems else "INVALID"))
    return 0 if not problems else 1


def cmd_status(args) -> int:
    cfg = load_settings()
    from .db import Database
    from .risk.manager import RiskManager
    db = Database(cfg.db_path)
    print(f"IMPERION v{VERSION} | {CORE_NAME} {CORE_VERSION} | {cfg.describe_mode()}")
    print("risk:", json.dumps(RiskManager(cfg, db).status(), default=str))
    for t in db.open_trades():
        print(f"  OPEN {t['side']} {t['symbol']} {t['style']} entry {t['entry']} stop {t['stop']} tp2 {t['tp2']} risk {t['risk_usdt']}")
    for e in db.events_since(now_ms() - 6 * 3600_000, ("WARNING", "ERROR", "CRITICAL"))[-10:]:
        print(f"  [{e['level']}] {e['kind']}: {e['message'][:140]}")
    return 0


def cmd_report(args) -> int:
    cfg = load_settings()
    setup_logging("WARNING")
    engine = _engine(cfg)
    if args.send:
        ok, detail = engine.send_report()
        if engine.tg is not None:
            engine.tg.flush()
        print("sent" if ok else f"NOT sent: {detail}")
        return 0 if ok else 1
    subject, text = engine.preview_report()
    print(subject, "\n", text)
    return 0


def cmd_halt(args) -> int:
    cfg = load_settings()
    from .db import Database
    from .risk.manager import RiskManager
    RiskManager(cfg, Database(cfg.db_path)).halt(args.reason or "manual halt (CLI)", "cli")
    print("Trading halted. Existing positions keep their exchange-side stops.")
    return 0


def cmd_resume(args) -> int:
    cfg = load_settings()
    from .db import Database
    from .risk.manager import RiskManager
    RiskManager(cfg, Database(cfg.db_path)).resume(by="cli")
    print("Halt cleared (equity baselines re-set on the next equity update).")
    return 0


def _ai(args, action: str) -> int:
    cfg = load_settings()
    setup_logging("WARNING")
    engine = _engine(cfg)
    if action == "status":
        print(json.dumps(engine.ai_summary(), indent=2, default=str))
    elif action == "run":
        print(json.dumps(engine.ai_run(), indent=2, default=str))
    elif action == "approve":
        print("approved" if engine.ai_approve(args.version) else "no such proposal")
    elif action == "rollback":
        print("rolled back to base weights (v%d)" % engine.ai_rollback())
    return 0


def cmd_selftest(args) -> int:
    setup_logging("ERROR")
    from .selftest import run_all
    print(f"IMPERION v{VERSION} selftest (in-process fake exchange, no network)")
    return 1 if run_all() else 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="imperion", description=f"IMPERION FINANCE v{VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    an = sub.add_parser("analyze")
    an.add_argument("--symbol", required=True); an.add_argument("--style", default="", help="scalp | day | swing (default: all active)")
    an.add_argument("--json", action="store_true"); an.set_defaults(fn=cmd_analyze)
    b = sub.add_parser("backtest")
    b.add_argument("--symbol", required=True); b.add_argument("--style", default="day"); b.add_argument("--days", type=float, default=14.0)
    b.add_argument("--step", type=int, default=1, help="evaluate every N-th trigger candle (faster, coarser)")
    b.add_argument("--json", action="store_true"); b.set_defaults(fn=cmd_backtest)
    se = sub.add_parser("sensitivity", help="replay history with every Director threshold nudged down/up")
    se.add_argument("--symbol", required=True); se.add_argument("--style", default="day"); se.add_argument("--days", type=float, default=30.0)
    se.add_argument("--step", type=int, default=2); se.add_argument("--json", action="store_true"); se.set_defaults(fn=cmd_sensitivity)
    op = sub.add_parser("optimize", help="Optuna Bayesian optimization (research only)")
    op.add_argument("--symbol", required=True); op.add_argument("--style", default="day"); op.add_argument("--days", type=float, default=30.0)
    op.add_argument("--trials", type=int, default=30); op.add_argument("--by-regime", action="store_true")
    op.add_argument("--json", action="store_true"); op.set_defaults(fn=cmd_optimize)
    sub.add_parser("preflight").set_defaults(fn=cmd_preflight)
    sub.add_parser("testnet-check").set_defaults(fn=cmd_testnet)
    al = sub.add_parser("approve-live", help="manual approval for live trading (needs testnet evidence)")
    al.add_argument("--operator", required=True); al.add_argument("--confirm", default="")
    al.set_defaults(fn=cmd_gate, action="approve")
    ie = sub.add_parser("import-evidence", help="verify a signed testnet evidence file (e.g. the GitHub artifact) and store it")
    ie.add_argument("file"); ie.set_defaults(fn=cmd_gate, action="import")
    ve = sub.add_parser("verify-evidence", help="verify a signed testnet evidence file (no database)")
    ve.add_argument("file"); ve.add_argument("--expect-sha256", default=""); ve.set_defaults(fn=cmd_verify_evidence)
    sub.add_parser("revoke-live").set_defaults(fn=cmd_gate, action="revoke")
    sub.add_parser("gate-status").set_defaults(fn=cmd_gate, action="status")
    sub.add_parser("status").set_defaults(fn=cmd_status)
    r = sub.add_parser("report"); r.add_argument("--send", action="store_true"); r.set_defaults(fn=cmd_report)
    h = sub.add_parser("halt"); h.add_argument("--reason", default=""); h.set_defaults(fn=cmd_halt)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    sub.add_parser("ai-status").set_defaults(fn=lambda a: _ai(a, "status"))
    sub.add_parser("ai-run").set_defaults(fn=lambda a: _ai(a, "run"))
    ap = sub.add_parser("ai-approve"); ap.add_argument("version", type=int); ap.set_defaults(fn=lambda a: _ai(a, "approve"))
    sub.add_parser("ai-rollback").set_defaults(fn=lambda a: _ai(a, "rollback"))
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    args = p.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except ValueError as exc:      # configuration errors
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
