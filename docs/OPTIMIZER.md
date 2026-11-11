# OPTIMIZER.md — v14.2 full injection + Optuna

## Features added

- **Deep ParameterSet injection** into `playbooks.py` (sweep_reversal fully uses injected ages, confirmation minima and score weights; other playbooks accept the parameter object).
- **Optuna Bayesian optimization** (`research/optuna_optimizer.py`).
- **Regime-based studies** (`--by-regime` runs a separate study for TREND / RANGE / VOLATILE / TRANSITION).

## Usage

```bash
pip install -r requirements.txt          # now includes optuna
python -m imperion optimize --symbol SOLUSDT --style day --days 40 --trials 40
python -m imperion optimize --symbol SOLUSDT --style day --days 40 --trials 25 --by-regime
```

## Safety

Research only. Results must still pass shadow → testnet → signed evidence → manual approve-live before any real-money order.

The live trading path, risk manager, lifecycle stops, and gate logic are unchanged.
