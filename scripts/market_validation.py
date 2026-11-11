#!/usr/bin/env python3
"""Measure confirmed price-action events against Binance USD-M historical candles.

This is a research-only tool: it never creates, cancels, or modifies orders.
Metrics are gross forward-return diagnostics, not a profitability guarantee.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from imperion.analysis.priceaction import build_map, smt_divergence
from imperion.exchange.rest import RestClient
from imperion.models import Candle


def fetch(rest: RestClient, symbol: str, timeframe: str, limit: int) -> List[Candle]:
    rows = rest.klines(symbol, timeframe, limit=min(1000, max(100, limit)))
    return [Candle.from_rest(row) for row in rows if len(row) >= 11]


def _summarise(rows: list[dict]) -> dict:
    if not rows:
        return {"events": 0, "hit_rate": None, "mean_forward_return_pct": None,
                "mean_mfe_pct": None, "mean_mae_pct": None}
    wins = [x for x in rows if x["forward_return_pct"] > 0]
    return {"events": len(rows), "hit_rate": round(len(wins) / len(rows), 4),
            "mean_forward_return_pct": round(sum(x["forward_return_pct"] for x in rows) / len(rows), 5),
            "mean_mfe_pct": round(sum(x["mfe_pct"] for x in rows) / len(rows), 5),
            "mean_mae_pct": round(sum(x["mae_pct"] for x in rows) / len(rows), 5)}


def evaluate(candles: List[Candle], horizon: int = 10, comparison: List[Candle] | None = None) -> dict:
    if len(candles) < 100:
        raise ValueError("at least 100 closed candles are required")
    if horizon < 1 or horizon > 100:
        raise ValueError("horizon must be between 1 and 100 candles")
    events: Dict[str, list] = defaultdict(list)
    start = 60
    end = len(candles) - horizon
    for i in range(start, end):
        window = candles[max(0, i - 299):i + 1]
        m = build_map(window)
        if not m.ok:
            continue
        candidates = []
        br = m.structure.last_break
        if br is not None and br.age == 0:
            candidates.append(("BOS" if br.kind == "BOS" else "CHoCH", 1 if br.direction == "UP" else -1))
        for sweep in m.sweeps:
            if sweep.age == 0:
                candidates.append(("LIQUIDITY_SWEEP", 1 if sweep.direction == "BULLISH" else -1))
        for zone in m.zones:
            flipped_now = zone.flipped_at == len(window) - 1
            created_now = zone.created == len(window) - 1
            if flipped_now or created_now:
                direction = zone.direction if zone.flipped_at is None else ("BEARISH" if zone.direction == "BULLISH" else "BULLISH")
                label = ("BREAKER" if zone.kind == "OB" else "IFVG") if flipped_now else ("OB" if zone.kind == "OB" else "FVG")
                candidates.append((label, 1 if direction == "BULLISH" else -1))
        if comparison is not None and i < len(comparison):
            comp_window = comparison[max(0, i - 299):i + 1]
            smt = smt_divergence(window, comp_window)
            if smt.get("available") and smt.get("direction") in ("BULLISH", "BEARISH"):
                candidates.append(("SMT", 1 if smt["direction"] == "BULLISH" else -1))
        future = candles[i + horizon]
        for name, sign in set(candidates):
            entry = candles[i].c
            fwd = (future.c / entry - 1.0) * 100.0 * sign
            path = candles[i + 1:i + horizon + 1]
            if sign > 0:
                mfe = (max(x.h for x in path) / entry - 1.0) * 100.0
                mae = (min(x.l for x in path) / entry - 1.0) * 100.0
            else:
                mfe = (entry / min(x.l for x in path) - 1.0) * 100.0
                mae = (entry / max(x.h for x in path) - 1.0) * 100.0
            events[name].append({"forward_return_pct": fwd, "mfe_pct": mfe, "mae_pct": mae})
    return {"candles": len(candles), "horizon_bars": horizon,
            "method": "confirmed_close_events; no future data in signal; gross returns exclude fees/slippage/funding",
            "signals": {name: _summarise(rows) for name, rows in sorted(events.items())}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--comparison", default="ETHUSDT", help="correlated instrument for SMT; empty disables SMT")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--output", default="market-validation.json")
    args = ap.parse_args()
    rest = RestClient("https://fapi.binance.com")
    primary = fetch(rest, args.symbol.upper(), args.timeframe, args.limit)
    secondary = fetch(rest, args.comparison.upper(), args.timeframe, args.limit) if args.comparison else None
    rep = evaluate(primary, args.horizon, secondary)
    rep.update({"symbol": args.symbol.upper(), "comparison": args.comparison.upper() if args.comparison else None,
                "timeframe": args.timeframe})
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
