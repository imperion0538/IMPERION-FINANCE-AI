# V14.2.1 HARDENED — Rebuild, Cloud Deployment and Live Readiness

## Build scope
This package is a cleaned rebuild of the V14.2.1 HARDENED source archive. Python bytecode caches and pytest caches are excluded. No exchange credentials, account identifiers, or private keys are included.

## Deployment target
- GitHub: upload the contents of this directory to a repository root (do not upload `.env`).
- Railway: deploy from the repository using the included `Dockerfile` and `railway.json`.
- Keep `numReplicas=1`. The trading engine is a single-writer process; do not scale it horizontally.
- Attach a persistent Railway Volume at `/data` and set `IMPERION_STORAGE_DIR=/data`.
- Configure secrets only in Railway Variables or GitHub Actions Secrets; never commit them.
- Configure health checks against `/health`; a healthy HTTP process is not proof of trading readiness.

## Safe rollout sequence
1. Start with `MODE=dry`, `KILL_SWITCH=true`, and no exchange API credentials. Confirm health, logs, persistence and graceful restart.
2. Use a Binance USDⓈ-M Futures Demo/Testnet account only. Create an API key with trading permission only; withdrawals must remain disabled. Restrict the key by IP where supported.
3. Run `python -m imperion testnet-check` with the explicit order lifecycle smoke test enabled and a small capped notional. Verify entry, protective stop, cancellation/close and cleanup on the demo account. Confirm the exchange-reported liquidation price and One-way / Single-Asset mode.
4. Preserve the signed Testnet evidence and import it into the same persistent database used by the deployment. The HMAC evidence secret must be at least 32 characters and must be kept secret.
5. Inspect `/status` and the live-gate reasons. Live approval is manual, time-limited, version-bound and evidence-hash-bound. Any evidence edit, new evidence, expiry, or revoked approval must close the gate.
6. Only after independent review of the Testnet run, set `MODE=live`, configure a dedicated low-limit key and explicitly approve with the CLI phrase documented in `docs/GO_LIVE.md`. Keep the kill switch and emergency stop procedure available.

## Validation performed for this rebuild
- `python -m compileall -q imperion scripts`: passed in the build environment.
- ZIP integrity (`ZipFile.testzip()`): passed for the source archive and rebuilt archive.
- Targeted pytest files completed successfully in separate invocations: core, live-gate, Price Action, radar, reports, research CLI, risk, safety regressions, self-test, Telegram, utility filters and webhook HTTP.
- A single full `pytest -q` invocation collected 208 tests but exceeded the execution environment's 120-second command limit. Therefore this rebuild does **not** claim a completed full-suite pass.
- No live Binance account was accessed. No real-money order was placed. Binance Testnet order lifecycle, Railway deployment, GitHub Actions run and cloud persistence have not been executed from this environment.

## Release status
GitHub/cloud deployment and real-money trading are **not certified by packaging**. Keep the live gate closed until the deployment-specific Testnet evidence and manual approval have been completed. Do not treat a passing local unit test as proof of exchange connectivity or profitable strategy performance.
