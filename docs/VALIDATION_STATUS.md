# V16.1 Validation Status

## Local validation (2026-11-11)
- `python scripts/release_check.py`: PASS
- `python -m compileall -q imperion scripts tests`: PASS
- `python -m imperion selftest`: PASS (all in-process fake-exchange safety scenarios)
- Pytest: 225/225 passed in five isolated shards:
  - Core / risk / engine / price action / V16 intelligence / V16.1 guards: 78 passed
  - Exchange data / live execution / learning: 38 passed
  - Reports / research CLI / liquidation / live gate: 54 passed
  - Safety regressions / selftest / Telegram / utility filters: 38 passed
  - Webhook / radar / unified features: 17 passed

## Not claimed as completed
- A single monolithic pytest invocation was not used as the final validation; tests were run in isolated shards.
- Docker CLI was unavailable in the build workspace; Docker image build and container healthcheck were not run locally.
- Binance Futures Demo/Testnet was not run because no user API credentials / external exchange session were supplied.
- No GitHub push, cloud deployment, or live order was performed.
- No profitability, win-rate, or positive-expectancy claim is made. The new toxicity thresholds are heuristic starting points and need out-of-sample calibration.

## Safe operating mode
`MODE=dry` remains the default. Keep live trading disabled until Demo/Testnet lifecycle validation, signed evidence, risk review and explicit operator approval are complete.
