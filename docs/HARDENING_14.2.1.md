# V14.2.1 HARDENED — engineering notes

## Included corrections

1. Removed a duplicated, unreachable and incorrectly indented block in `analysis/priceaction/playbooks.py` that prevented imports and test collection.
2. Updated the Director test double to accept the optional injected `ParameterSet` argument.
3. Changed Binance USDⓈ-M Multi-Assets account-mode lookup failure from warning to preflight error. Isolated-margin execution now requires positively verified Single-Asset mode.
4. Made runtime health reflect startup readiness. `/health` and `/` return HTTP 503 until `Engine._ready` is true, then return 200.
5. Hardened guardian reconciliation: an exchange-visible stop must match the persisted stop trigger and cover the current live position quantity (or use `closePosition`). Stale/undersized/wrong-trigger stops are replaced before stale orders are cancelled. If a replacement cannot be confirmed, the existing emergency-close path is invoked.
6. Added a Docker/Railway entrypoint that repairs ownership of the mounted `/data` volume while privileged, then uses `gosu` to run the application as UID 10001.
7. Aligned release metadata, README, changelog and deployment documentation at `14.2.1`.

## Safety defaults

- `.env.example` remains `MODE=dry`.
- Live trading requires the existing signed Testnet evidence, its matching manual approval and the explicit real-money confirmation.
- Do not store Binance credentials in GitHub files or the ZIP. Add secrets only through the deployment platform's secret-variable facility.
- Railway should use one replica and a persistent volume mounted at `/data`.

## Additional research instrumentation

8. Added confirmed-swing SMT divergence as an explicit research/confirmation helper. It refuses short or time-misaligned series and does not inspect unconfirmed right-side swings.
9. Added an aggTrade-derived rolling volume profile with signed delta/CVD, delta ratio, POC bin and per-price-level buy/sell quantities. Live order-flow now exposes these values separately from REST candle taker-buy delta and includes the footprint level count.
10. Added `scripts/market_validation.py` to fetch public USD-M klines and report forward-return, MFE and MAE diagnostics for BOS/CHoCH, liquidity sweeps, OB/Breaker, FVG/IFVG and optional SMT events. It is research-only and excludes fees, slippage and funding; results must not be interpreted as a profitability guarantee.
11. Added `scripts/orderflow_capture.py` for a read-only public aggTrade capture and CVD/footprint report, with no API credentials and no order capability.

## Verification performed in the build workspace

- `python -m compileall -q imperion scripts tests`: PASS.
- `python scripts/release_check.py`: PASS.
- `python -m imperion selftest`: PASS (all built-in fake-exchange scenarios).
- Focused regression tests for guardian stop repair, fail-closed Multi-Assets preflight, readiness, Director compatibility and Price Action: PASS.
- Focused Price Action and research/order-flow tests include confirmed-swing SMT, aggTrade delta/CVD and footprint profile coverage.
- All 208 collected tests passed across four isolated test groups in the build workspace (70 + 62 + 27 + 49). A single-process run exceeded the available execution window, so CI now runs the same complete set in four independently reported groups across Python 3.11/3.12/3.13.
- Docker build, Railway deployment and live Binance Testnet execution were not run in this environment. These require the target runner, persistent-volume validation and user-controlled Demo credentials.

## Deploy

See `docs/GO_LIVE.md`, `.env.example`, `Dockerfile` and `railway.json`. Keep `MODE=dry` until Demo/Testnet evidence and all deployment checks have passed. Do not enable live trading as part of the initial deployment.
