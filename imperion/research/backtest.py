"""Candle-replay backtester: runs the REAL Director (price-action maps, agents, playbooks, planner, consensus, grades)
over history, candle by candle, with no look-ahead.

What it is honest about:
  * only candle-derived agents can be replayed; the live microstructure agent (book, whale prints, footprint) and
    funding need live streams and report "unavailable", exactly as the live engine does without them;
  * the AI layer (context filter, win model) and account risk limits are not applied: this measures the analysis;
  * fills are modelled like the shadow tracker: entry at the signal close, stop first when stop and target fall in
    the same candle, TP1 takes half and moves the stop to break-even, round-trip fees + slippage charged in R;
  * one position at a time per (symbol, style), with the style cooldown.
A backtest is a sanity check of the logic on past data, not a promise about future results.
"""
from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import STYLES, TIMEFRAMES, Settings
from ..core.director import Director
from ..core.planner import round_trip_cost_pct
from ..data.candles import DEFAULT_MIN_HISTORY, MIN_HISTORY
from ..data.microstructure import BookStore, TradeStore
from ..data.universe import DerivativesStore
from ..learning.shadow import mark_to_market, simulate_outcome
from ..models import Candle
from ..util import iso, tf_ms

WINDOW = 500            # candles per series visible to the analysis (same as the live bootstrap)
WARMUP_CANDLES = 320
MAX_PAGES = 400
LEADER = "BTCUSDT"
_CODE = re.compile(r"([A-Z][A-Z0-9]*_[A-Z0-9_]+)")


class ReplayStore:
    """CandleStore look-alike that only reveals candles closed before `cursor` (no look-ahead)."""

    def __init__(self, series: Dict[Tuple[str, str], List[Candle]], price_tf: str):
        self._s = {k: sorted(v, key=lambda c: c.t) for k, v in series.items()}
        self._ct = {k: [c.ct for c in v] for k, v in self._s.items()}
        self.price_tf = price_tf
        self.cursor = 0

    def get(self, symbol: str, tf: str, n: Optional[int] = None) -> List[Candle]:
        key = (symbol, tf)
        data = self._s.get(key)
        if not data:
            return []
        hi = bisect_left(self._ct[key], self.cursor)
        return data[max(0, hi - (n or WINDOW)):hi]

    def count(self, symbol: str, tf: str) -> int:
        key = (symbol, tf)
        return bisect_left(self._ct[key], self.cursor) if key in self._ct else 0

    def last_open(self, symbol: str, tf: str) -> Optional[int]:
        cs = self.get(symbol, tf, 1)
        return cs[-1].t if cs else None

    def last_price(self, symbol: str) -> Optional[float]:
        cs = self.get(symbol, self.price_tf, 1)
        return cs[-1].c if cs else None

    def forming(self, symbol: str, tf: str) -> None:
        return None

    def has_gap(self, symbol: str, tf: str, n: int = 40) -> bool:
        return False


def fetch_history(rest, symbol: str, tf: str, start_ms: int, end_ms: int) -> List[Candle]:
    """Closed candles of [start_ms, end_ms) via paged REST klines."""
    step, cursor = tf_ms(tf), start_ms
    seen: Dict[int, Candle] = {}
    for _ in range(MAX_PAGES):
        rows = rest.klines(symbol, tf, limit=1500, start_time=cursor, end_time=end_ms)
        if not rows:
            break
        for row in rows:
            try:
                c = Candle.from_rest(row)
            except (IndexError, TypeError, ValueError):
                continue
            if c.ct < end_ms and c.t >= start_ms:
                seen[c.t] = c
        nxt = int(rows[-1][0]) + step
        if nxt <= cursor or nxt >= end_ms:
            break
        cursor = nxt
    return [seen[t] for t in sorted(seen)]


def replay_timeframes(style_name: str) -> Tuple[str, ...]:
    """The style's timeframes plus every higher one (they feed the multi-timeframe alignment)."""
    style = STYLES[style_name]
    floor = tf_ms(style.trigger_tf)
    return tuple(tf for tf in TIMEFRAMES if tf_ms(tf) >= floor or tf in style.all_tfs)


def download(rest, symbol: str, style_name: str, start_ms: int, end_ms: int) -> Dict[Tuple[str, str], List[Candle]]:
    """Everything a replay of (symbol, style) needs, including warm-up history and the BTC leader series."""
    style = STYLES[style_name]
    series: Dict[Tuple[str, str], List[Candle]] = {}
    for tf in replay_timeframes(style_name):
        series[(symbol, tf)] = fetch_history(rest, symbol, tf, start_ms - WARMUP_CANDLES * tf_ms(tf), end_ms)
    if symbol != LEADER:
        for tf in style.htf_bias:
            series[(LEADER, tf)] = fetch_history(rest, LEADER, tf, start_ms - WARMUP_CANDLES * tf_ms(tf), end_ms)
    return series


def _stats(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rs = [t["r"] for t in trades]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    by: Dict[str, Dict[str, Any]] = {}
    for key in ("status", "playbook", "grade", "regime", "side"):
        groups: Dict[str, List[float]] = {}
        for t in trades:
            groups.setdefault(str(t.get(key)), []).append(t["r"])
        by[key] = {k: {"n": len(v), "expectancy_r": round(sum(v) / len(v), 3)} for k, v in sorted(groups.items())}
    return {
        "trades": n, "win_rate": round(len(wins) / n, 3) if n else None, "expectancy_r": round(sum(rs) / n, 3) if n else None,
        "total_r": round(sum(rs), 3), "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else None,
        "max_drawdown_r": round(dd, 3), "avg_win_r": round(sum(wins) / len(wins), 3) if wins else None,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else None, "by": by,
    }


def run_backtest(series: Dict[Tuple[str, str], List[Candle]], symbol: str, style_name: str, cfg: Settings, *,
                 start_ms: Optional[int] = None, step: int = 1, weights: Optional[Dict[str, float]] = None,
                 thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Replay `series` ({(symbol, tf): candles}) for one (symbol, style). Returns {summary, trades, funnel, rejections}."""
    style = STYLES[style_name]
    trig = sorted(series.get((symbol, style.trigger_tf), []), key=lambda c: c.t)
    store = ReplayStore(series, style.trigger_tf)
    director = Director(cfg, store, TradeStore(cfg.whale_floor_usdt), BookStore(), DerivativesStore(),
                        weights_provider=lambda: (0, dict(weights or {})), threshold_overrides=thresholds)
    cost_pct = round_trip_cost_pct(cfg)
    hold = max(1, style.max_hold_minutes * 60_000 // tf_ms(style.trigger_tf))
    funnel = {"evaluated": 0, "playbook": 0, "planned": 0, "approved": 0, "traded": 0, "unresolved_at_end": 0}
    rejections: Counter = Counter()
    trades: List[Dict[str, Any]] = []
    busy_until = 0
    for idx in range(0, len(trig), max(1, step)):
        c = trig[idx]
        if start_ms is not None and c.ct < start_ms:
            continue
        store.cursor = cursor = c.ct + 1
        if cursor <= busy_until:
            continue
        if any(store.count(symbol, tf) < MIN_HISTORY.get(tf, DEFAULT_MIN_HISTORY) for tf in style.all_tfs):
            continue
        funnel["evaluated"] += 1
        an = director.analyze(symbol, style_name, None, now=cursor)
        if an.playbooks:
            funnel["playbook"] += 1
        if an.setup is not None:
            funnel["planned"] += 1
        if not an.approved:
            for r in an.reasons[:3]:
                m = _CODE.search(str(r))
                rejections[m.group(1) if m else str(r)[:40]] += 1
            continue
        funnel["approved"] += 1
        setup = an.setup
        future = trig[idx + 1: idx + 1 + hold]
        cost_r = (cost_pct / 100 * setup.entry) / setup.risk_per_unit
        res = simulate_outcome(setup.side, setup.entry, setup.stop, setup.tp1, setup.tp2, future, cost_r)
        if res is None:
            if len(future) < hold:               # data ends before the trade can resolve: not a result
                funnel["unresolved_at_end"] += 1
                break
            tp1_hit = any((x.h >= setup.tp1) if setup.side == "LONG" else (x.l <= setup.tp1) for x in future)
            res = {"status": "EXPIRED", "r": mark_to_market(setup.side, setup.entry, setup.stop, future[-1].c, tp1_hit, setup.tp1, cost_r),
                   "bars": len(future)}
        funnel["traded"] += 1
        exit_ms = future[res["bars"] - 1].ct if res.get("bars") else cursor
        busy_until = max(exit_ms, cursor + style.cooldown_minutes * 60_000)
        trades.append({"ts": iso(cursor), "side": setup.side, "entry": setup.entry, "stop": setup.stop, "tp1": setup.tp1, "tp2": setup.tp2,
                       "status": res["status"], "r": round(res["r"], 4), "bars": res.get("bars"), "quality": an.quality, "grade": an.grade,
                       "playbook": setup.playbook, "regime": an.regime.get("regime"), "session": an.regime.get("session")})
    first = next((x for x in trig if start_ms is None or x.ct >= start_ms), None)
    return {
        "symbol": symbol, "style": style_name, "trigger_tf": style.trigger_tf, "from": iso(first.t) if first else None,
        "to": iso(trig[-1].ct) if trig else None, "cost_pct_round_trip": cost_pct, "funnel": funnel,
        "rejections": dict(rejections.most_common(12)), "summary": _stats(trades), "trades": trades,
        "note": "candle-derived agents only (order book / whale flow / footprint / funding need live streams); "
                "AI filters and account limits are not applied; past results do not predict future results",
    }


def format_report(rep: Dict[str, Any]) -> str:
    s, f = rep["summary"], rep["funnel"]
    lines = [f"BACKTEST {rep['symbol']} / {rep['style']} ({rep['trigger_tf']})  {rep['from']} -> {rep['to']}",
             f"  evaluated {f['evaluated']} | playbook {f['playbook']} | planned {f['planned']} | approved {f['approved']} | traded {f['traded']}",
             f"  trades {s['trades']} | win rate {s['win_rate']} | expectancy {s['expectancy_r']} R | total {s['total_r']} R | "
             f"profit factor {s['profit_factor']} | max drawdown {s['max_drawdown_r']} R",
             f"  round-trip cost {rep['cost_pct_round_trip']:.2f}% of notional charged on every trade"]
    if rep.get("rejections"):
        lines.append("  most common rejections: " + ", ".join(f"{k} x{v}" for k, v in rep["rejections"].items()))
    for key, groups in s["by"].items():
        if groups:
            lines.append(f"  by {key}: " + ", ".join(f"{k} n={v['n']} {v['expectancy_r']:+.2f}R" for k, v in groups.items()))
    for t in rep["trades"][-15:]:
        lines.append(f"    {t['ts']} {t['side']:5} {t['playbook']:<16} {t['grade']:<2} q={t['quality']:.0f} {str(t['regime']):<11} {t['status']:<13} {t['r']:+.2f}R")
    lines.append("  " + rep["note"])
    return "\n".join(lines)
