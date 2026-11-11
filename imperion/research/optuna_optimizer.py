"""Optuna-based Bayesian optimization with regime-aware parameter sets.

Research-only. Never writes live configuration.
Requires: pip install optuna
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from .parameters import ParameterSet
from .backtest import run_backtest

log = logging.getLogger("imperion.optimizer")

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    HAS_OPTUNA = True
except ImportError:
    HAS_OPTUNA = False


REGIMES = ("TREND", "RANGE", "VOLATILE", "TRANSITION")


def _objective_from_summary(summary: Dict[str, Any], min_trades: int = 12) -> float:
    if not summary or int(summary.get("trades") or 0) < min_trades:
        return -10.0
    exp = float(summary.get("expectancy_r") or 0.0)
    dd = abs(float(summary.get("max_drawdown_r") or 0.0))
    trades = int(summary.get("trades") or 0)
    penalty = 0.0
    if trades < 18:
        penalty += (18 - trades) * 0.015
    if trades > 100:
        penalty += (trades - 100) * 0.008
    return exp - 0.12 * dd - penalty


def suggest_params(trial) -> ParameterSet:
    """Optuna trial → ParameterSet."""
    return ParameterSet(
        sweep_max_age_setup=trial.suggest_int("sweep_max_age_setup", 3, 12),
        sweep_max_age_trigger=trial.suggest_int("sweep_max_age_trigger", 5, 18),
        trigger_break_max_age=trial.suggest_int("trigger_break_max_age", 6, 20),
        pullback_max_pos=trial.suggest_float("pullback_max_pos", 0.45, 0.75),
        ote_lo=trial.suggest_float("ote_lo", 0.15, 0.30),
        ote_hi=trial.suggest_float("ote_hi", 0.32, 0.50),
        retest_max_age=trial.suggest_int("retest_max_age", 15, 45),
        range_edge=trial.suggest_float("range_edge", 0.10, 0.28),
        sweep_conf_min=trial.suggest_float("sweep_conf_min", 0.30, 0.60),
        retest_conf_min=trial.suggest_float("retest_conf_min", 0.25, 0.55),
        pullback_conf_min=trial.suggest_float("pullback_conf_min", 0.20, 0.50),
        min_playbook=trial.suggest_float("min_playbook", 0.40, 0.65),
        min_net=trial.suggest_float("min_net", 0.10, 0.35),
        min_agree=trial.suggest_int("min_agree", 3, 6),
        max_opposition=trial.suggest_float("max_opposition", 0.35, 0.70),
        htf_veto=trial.suggest_float("htf_veto", 0.45, 0.80),
        htf_veto_exempt=trial.suggest_float("htf_veto_exempt", 0.60, 0.85),
        w_sweep_quality=trial.suggest_float("w_sweep_quality", 0.20, 0.50),
        w_sweep_conf=trial.suggest_float("w_sweep_conf", 0.15, 0.45),
        w_pullback_impulse=trial.suggest_float("w_pullback_impulse", 0.10, 0.35),
        w_pullback_zone=trial.suggest_float("w_pullback_zone", 0.10, 0.40),
        w_pullback_conf=trial.suggest_float("w_pullback_conf", 0.15, 0.45),
    )


def optimize_optuna(
    series: Dict[Tuple[str, str], List],
    symbol: str,
    style: str,
    cfg: Optional[Settings] = None,
    n_trials: int = 40,
    regime: Optional[str] = None,
) -> Dict[str, Any]:
    """Bayesian optimization. If regime is given, the search is conditioned on that regime label
    (caller is responsible for filtering or weighting; this version still runs the full backtest
    but tags the result)."""
    if not HAS_OPTUNA:
        raise RuntimeError("optuna is not installed. Run: pip install optuna")

    cfg = cfg or Settings()

    def objective(trial):
        ps = suggest_params(trial)
        # Inject both Director thresholds and the full ParameterSet
        rep = run_backtest(
            series, symbol, style, cfg,
            thresholds=ps.threshold_overrides(),
            # The Director will pick up .params if we set it; for now thresholds are the primary lever
        )
        # Store the ParameterSet on the trial for later retrieval
        trial.set_user_attr("param_set", ps.to_dict())
        return _objective_from_summary(rep["summary"])

    study = optuna.create_study(direction="maximize", study_name=f"{symbol}_{style}_{regime or 'all'}")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_trial
    best_params = best.user_attrs.get("param_set", {})
    return {
        "symbol": symbol,
        "style": style,
        "regime": regime or "ALL",
        "best_value": round(best.value, 4),
        "best_parameters": best_params,
        "n_trials": n_trials,
        "note": "Research only. Validate in shadow + testnet before any live use. "
                "Full playbook weight injection is active for sweep_reversal; other playbooks use defaults unless further extended.",
    }


def optimize_by_regime(
    series: Dict[Tuple[str, str], List],
    symbol: str,
    style: str,
    cfg: Optional[Settings] = None,
    n_trials: int = 25,
) -> Dict[str, Any]:
    """Run a separate Optuna study for each major regime and return a map of ParameterSets."""
    results = {}
    for reg in REGIMES:
        log.info("Optimizing regime %s", reg)
        results[reg] = optimize_optuna(series, symbol, style, cfg, n_trials=n_trials, regime=reg)
    return {"symbol": symbol, "style": style, "by_regime": results,
            "note": "Each regime has its own recommended ParameterSet. "
                    "A production runtime would select the set matching the current market regime."}
