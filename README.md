# IMPERION FINANCE AI V17.0.0
## Unified Institutional Crypto Market Intelligence

This repository consolidates the strongest architecture from:
- **V16.1.0:** market intelligence, confluence, adaptive thresholds, toxicity and inventory guards.
- **V15.2.0:** advanced Price Action and Order Flow coverage, including SMT agent, CVD/delta, volume profile and eight playbooks.
- **V14.3.0:** hardened execution lifecycle, fail-closed risk controls, Testnet evidence, live approval gate and production runbook.

> **Safety first:** default mode is `dry`. This package does not enable real-money trading by default. Do not add API credentials until you have reviewed the security documentation. Never enable withdrawals on an exchange API key. A successful compile or unit test is not proof of profitability or live-exchange readiness.

## Included capabilities
- Binance USDⓈ-M market-data and execution adapters.
- Multi-timeframe analysis and SMC/ICT Price Action.
- BOS/CHoCH, liquidity pools/sweeps, OB, FVG/IFVG, breaker, SMT, IDM, OTE, mitigation, CISD and session models.
- Order Flow: taker delta, CVD, divergence, absorption, volume profile/POC, book imbalance, whale prints and stacked imbalance.
- Confluence scoring, displacement quality, volatility-adaptive thresholds and research-only MMBM/MMSM candidates.
- Toxicity/adverse-selection guard and correlated portfolio inventory guard.
- Director/agent consensus, playbooks, quality grading and veto layers.
- Persistent risk limits, liquidation checks, exchange-side protective stops, guardian/reconciliation, kill switch and Testnet-to-Live gate.
- Backtesting, sensitivity analysis, optimizer, walk-forward and shadow performance.

## Conservative defaults
See `imperion/config.py` and `.env.example`. Default mode is `dry`; do not change this for first run. Risk defaults are intentionally conservative and are not a promise of safety or return.

## Quick start (Python 3.11–3.13)
```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -m imperion selftest
python -m pytest -q
```

## Analyze without placing orders
```bash
python -m imperion analyze --symbol BTCUSDT --style day
python -m imperion backtest --symbol BTCUSDT --style day --days 30 --step 2
```

## Docker
```bash
docker build -t imperion-finance:17.0.0 .
docker run --rm -p 8080:8080 --env-file .env -v imperion-data:/data imperion-finance:17.0.0
```
Health endpoint: `/health`. Mount persistent storage at `/data`. The image is designed to run as a non-root user.

## GitHub upload
1. Create an empty GitHub repository.
2. Extract this ZIP and upload the **contents** of this folder to the repository root (not the enclosing folder).
3. Push the default branch; GitHub Actions runs CI.
4. Add only required secrets under Settings → Secrets and variables → Actions. Do not commit `.env`, API keys, evidence secrets or database files.

## Cloud deployment
Use the included `Dockerfile`, `render.yaml` and deployment guide at `docs/CLOUD_DEPLOYMENT_TR.md`. Configure persistent disk/volume at `/data`, health check `/health`, and environment variable `MODE=dry`. Cloud provider deployment does not itself authorize Live trading.

## Live trading gate
Live execution requires all existing code-level conditions, fresh signed Binance Futures Testnet evidence, an approval bound to that evidence, explicit operator confirmation and exchange-side safeguards. First deploy in `dry`, then validate on Testnet. Never bypass the gate.

## Validation status
This archive has been checked for Python syntax and release metadata. Full test results are recorded in `BUILD_VALIDATION.txt`. CI, external dependency audit, cloud runtime, Binance Testnet and live exchange behavior must still be verified in the target environment.

## License
Review and add the appropriate license before public redistribution.
