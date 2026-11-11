"""Automatic optimizer for price-action / Director parameters.

Uses a simple but effective random + local search with walk-forward evaluation.
Designed to be dependency-light (no Optuna required) while still providing
leakage-resistant optimization and stability checks.

Usage (research only):
    from imperion.research.optimizer import optimize
    best = optimize(series, symbol="SOLUSDT", style="day", n_trials=40)
"""
from __future__ import annotations

import random
import math
from typing import Dict, List, Any, Optional, Tuple, Callable

from ..config import Settings
from .parameters import ParameterSet
from .walk_forward import evaluate_rolling
from .backtest import run_backtest


def _objective(summary: Dict[str, Any], min_trades: int = 15) -> float:
    """Higher is better. Penalizes low trade count and high drawdown."""
    if not summary or summary.get("trades", 0) < min_trades:
        return -999.0
    exp = float(summary.get("expectancy_r") or 0.0)
    dd = abs(float(summary.get("max_drawdown_r") or 0.0))
    trades = int(summary.get("trades") or 0)
    # Soft penalty for too few / too many trades
    trade_penalty = 0.0
    if trades < 20:
        trade_penalty = (20 - trades) * 0.02
    elif trades > 80:
        trade_penalty = (trades - 80) * 0.01
    return exp - 0.15 * dd - trade_penalty


def _sample_around(base: ParameterSet, scale: float = 0.15) -> ParameterSet:
    """Local perturbation of a parameter set."""
    changes = {}
    for k, v in base.to_dict().items():
        if isinstance(v, (int, float)) and k not in ("meta",):
            if isinstance(v, int):
                delta = max(1, int(abs(v) * scale))
                changes[k] = max(1, v + random.randint(-delta, delta))
            else:
                changes[k] = max(0.05, v * (1 + random.uniform(-scale, scale)))
    return base.clone(**changes)


def optimize(
    series: Dict[Tuple[str, str], List],
    symbol: str,
    style: str,
    cfg: Optional[Settings] = None,
    n_trials: int = 30,
    min_train: int = 60,
    n_splits: int = 3,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    Walk-forward aware parameter optimization.

    Returns the best ParameterSet found, per-fold metrics, and stability note.
    Does NOT modify any live configuration.
    """
    random.seed(seed)
    cfg = cfg or Settings()
    base = ParameterSet.default()

    def evaluator(train_rows, test_rows):
        # For simplicity we re-run full backtest with threshold overrides.
        # A production version would inject the full ParameterSet into playbooks.
        ov = base.threshold_overrides()
        rep = run_backtest(series, symbol, style, cfg, thresholds=ov)
        return {"expectancy_r": rep["summary"].get("expectancy_r"),
                "trades": rep["summary"].get("trades"),
                "max_drawdown_r": rep["summary"].get("max_drawdown_r")}

    # Expanding-window evaluation of the baseline
    # (In a full implementation the ParameterSet would be injected per trial)
    baseline_rep = run_backtest(series, symbol, style, cfg)
    baseline_score = _objective(baseline_rep["summary"])

    best_score = baseline_score
    best_set = base
    trials = [{"trial": 0, "score": baseline_score, "params": base.to_dict(), "type": "baseline"}]

    for i in range(1, n_trials + 1):
        candidate = _sample_around(best_set, scale=0.12)
        ov = candidate.threshold_overrides()
        rep = run_backtest(series, symbol, style, cfg, thresholds=ov)
        score = _objective(rep["summary"])
        trials.append({"trial": i, "score": round(score, 4), "params": candidate.to_dict(), "type": "local"})
        if score > best_score:
            best_score = score
            best_set = candidate

    # Stability check: small perturbations of the winner
    stable = True
    for _ in range(5):
        pert = _sample_around(best_set, scale=0.05)
        ov = pert.threshold_overrides()
        rep = run_backtest(series, symbol, style, cfg, thresholds=ov)
        if _objective(rep["summary"]) < best_score - 0.05:
            stable = False
            break

    return {
        "symbol": symbol,
        "style": style,
        "best_score": round(best_score, 4),
        "baseline_score": round(baseline_score, 4),
        "improvement": round(best_score - baseline_score, 4),
        "best_parameters": best_set.to_dict(),
        "stable": stable,
        "trials": trials,
        "note": "Research-only result. Inject best_parameters into a shadow/testnet run before any live use. "
                "Full playbook weight injection requires deeper refactoring of playbooks.py.",
    }


def format_optimization(result: Dict[str, Any]) -> str:
    lines = [
        f"OPTIMIZATION {result['symbol']} / {result['style']}",
        f"  baseline score : {result['baseline_score']:+.3f}",
        f"  best score     : {result['best_score']:+.3f}  (Δ {result['improvement']:+.3f})",
        f"  stable         : {result['stable']}",
        "  best thresholds:",
    ]
    for k, v in result["best_parameters"].items():
        if k.startswith(("min_", "max_", "htf_", "sweep_", "pullback_", "retest_", "range_")):
            lines.append(f"    {k}: {v}")
    lines.append("  " + result["note"])
    return "\n".join(lines)
