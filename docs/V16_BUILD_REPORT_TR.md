# V16.0.0 build and delivery report

## Source baseline

- V15.0.0 Unified archive supplied in this conversation.
- Existing Binance execution safety, Guardian, risk gates, persistence, learning isolation and cloud deployment assets retained.

## V16 engineering changes

- SMT divergence allowlist is queried by Director on the same setup timeframe; unavailable or unaligned pair data is not treated as confirmation.
- London, New York and Asia session windows use IANA timezones. London Open, NY AM and actual London/New York overlap are tagged with DST-aware timestamps.
- Internal structure is calculated from the minor swing scale independently of external structure. Advanced Inducement Sweep and Session CISD require a recent external sweep and same-direction trigger-timeframe internal CHoCH.
- Displacement quality includes body/range ratio, ATR multiple, volume proxy and consecutive directional candle continuity.
- Triple confluence combines OTE, quality IDM and nearby mitigation evidence with bounded score contribution.
- Session CISD requires sweep quality >= 0.65 and follow-through >= 0.60. Missing momentum/trend alignment reduces the score.
- AggTrade delta/CVD and candle-delta divergence are separate evidence sources; the latter is explicitly labeled a candle proxy.
- Adaptive thresholds tighten selected quality gates in volatile regimes and do not relax account risk limits.
- MMBM/MMSM candidate detection is research-only and cannot independently create a trade.
- OTE/Mitigation plans expose the 50% zone equilibrium as a reference; existing market execution is not silently changed into a limit order.
- Learning core version is isolated from V15 so old score semantics are not mixed with V16.

## Validation results

- 218 pytest tests passed across isolated groups.
- Python compileall passed.
- Release metadata check passed.
- In-process fake-exchange safety selftest passed.
- Docker image build was not run because Docker CLI is not available in this workspace.
- GitHub Actions and authenticated Binance Testnet were not run from this workspace.

## Deployment instructions

1. Push the extracted project root to a new GitHub repository.
2. Wait for `.github/workflows/ci.yml` matrix, security audit and Docker job to pass.
3. Deploy the repository using the included `Dockerfile`, `railway.json` or `render.yaml`.
4. Mount persistent storage at `/data`, keep one replica, and set `MODE=dry`.
5. Verify `/health`, restart recovery and logs.
6. Run the configured Binance Testnet workflow with test credentials and verify signed evidence.
7. Do not enable live trading until the existing manual live gate and production approval requirements are met.
