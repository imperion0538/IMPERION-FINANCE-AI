"""Parameter sensitivity: how much do the results depend on the Director's thresholds?

The same history is replayed with each threshold nudged down and up one step. A setup logic whose expectancy flips
sign or whose trade count explodes after a small nudge is fragile; a robust one changes gradually. This is a
diagnostic, not an optimiser: it never changes the live configuration.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..core.policy import DEFAULT_THRESHOLDS
from ..models import Candle
from .backtest import run_backtest

STEPS: Dict[str, float] = {"min_playbook": 0.05, "min_net": 0.04, "min_agree": 1, "max_opposition": 0.10, "htf_veto": 0.10}


def variants() -> List[Tuple[str, Dict[str, float]]]:
    out: List[Tuple[str, Dict[str, float]]] = [("baseline", {})]
    for k, step in STEPS.items():
        for sign in (-1, 1):
            out.append((f"{k}{'-' if sign < 0 else '+'}", {k: round(DEFAULT_THRESHOLDS[k] + sign * step, 4)}))
    return out


def run_sensitivity(series: Dict[Tuple[str, str], List[Candle]], symbol: str, style: str, cfg: Settings, *,
                    start_ms: Optional[int] = None, step: int = 2) -> Dict[str, Any]:
    rows = []
    for name, ov in variants():
        rep = run_backtest(series, symbol, style, cfg, start_ms=start_ms, step=step, thresholds=ov or None)
        s = rep["summary"]
        rows.append({"variant": name, "overrides": ov, "trades": s["trades"], "win_rate": s["win_rate"], "expectancy_r": s["expectancy_r"],
                     "total_r": s["total_r"], "max_drawdown_r": s["max_drawdown_r"], "approved": rep["funnel"]["approved"]})
    base = rows[0]
    flips = [r["variant"] for r in rows[1:] if base["expectancy_r"] is not None and r["expectancy_r"] is not None
             and (base["expectancy_r"] > 0) != (r["expectancy_r"] > 0)]
    swings = [r["variant"] for r in rows[1:] if base["trades"] and abs(r["trades"] - base["trades"]) / base["trades"] > 0.5]
    verdict = "insufficient trades" if base["trades"] < 20 else ("FRAGILE" if flips or swings else "STABLE")
    return {"symbol": symbol, "style": style, "rows": rows, "sign_flips": flips, "large_count_swings": swings, "verdict": verdict,
            "note": "diagnostic on past data only; the live configuration is unchanged"}


def format_sensitivity(rep: Dict[str, Any]) -> str:
    lines = [f"SENSITIVITY {rep['symbol']} / {rep['style']} — verdict: {rep['verdict']}",
             f"  {'variant':<18}{'trades':>7}{'win%':>7}{'exp R':>8}{'total R':>9}{'maxDD R':>9}"]
    for r in rep["rows"]:
        wr = f"{r['win_rate'] * 100:.0f}" if r["win_rate"] is not None else "-"
        ex = f"{r['expectancy_r']:+.2f}" if r["expectancy_r"] is not None else "-"
        lines.append(f"  {r['variant']:<18}{r['trades']:>7}{wr:>7}{ex:>8}{r['total_r']:>+9.2f}{r['max_drawdown_r']:>9.2f}")
    if rep["sign_flips"]:
        lines.append("  expectancy changes sign with: " + ", ".join(rep["sign_flips"]))
    if rep["large_count_swings"]:
        lines.append("  trade count moves >50% with: " + ", ".join(rep["large_count_swings"]))
    lines.append("  " + rep["note"])
    return "\n".join(lines)
