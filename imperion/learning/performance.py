"""Performance classes. Simulated and exchange-confirmed results are measured separately and NEVER pooled.

  shadow   hypothetical outcome of every executable plan (candle replay, conservative fills)
  dry      paper trades of MODE=dry (simulated fills on real prices)
  testnet  trades confirmed by Binance Futures Demo fills
  live     trades confirmed by real-money fills
Only `live` says anything about real-money performance; `testnet` proves the execution path, not the edge.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

CLASSES = ("shadow", "dry", "testnet", "live")


def _stats(r_values: List[float], pnl: Optional[List[float]] = None) -> Dict[str, Any]:
    n = len(r_values)
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r < 0]
    out: Dict[str, Any] = {
        "samples": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "expectancy_r": round(sum(r_values) / n, 4) if n else None,
        "total_r": round(sum(r_values), 4),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
    }
    if pnl is not None:
        out["pnl_usdt"] = round(sum(pnl), 4)
    return out


def performance_report(shadow_rows: Iterable[Mapping[str, Any]], trade_rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """`shadow_rows`: resolved shadows (r_result). `trade_rows`: closed trades (mode, r_multiple, pnl_usdt).

    Trades closed without verified fills (EXIT_UNVERIFIED, r_multiple NULL) are counted separately, never as zero.
    """
    out: Dict[str, Any] = {"shadow": _stats([float(r["r_result"]) for r in shadow_rows if r.get("r_result") is not None])}
    rows = list(trade_rows)
    for cls in ("dry", "testnet", "live"):
        mine = [r for r in rows if r.get("mode") == cls]
        verified = [r for r in mine if r.get("r_multiple") is not None]
        st = _stats([float(r["r_multiple"]) for r in verified], [float(r.get("pnl_usdt") or 0.0) for r in verified])
        st["unverified"] = len(mine) - len(verified)
        out[cls] = st
    out["policy"] = "classes are reported separately and never pooled; only 'live' measures real-money performance"
    return out
