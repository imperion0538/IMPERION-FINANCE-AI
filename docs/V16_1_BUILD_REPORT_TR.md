# V16.1 Build Report

- Version: 17.0.0
- Learning core compatibility: retained `agents-1-v16-market-intelligence` because V16.1 changes risk gating, not the underlying scoring feature semantics.
- Base: V16 Advanced Market Intelligence
- Reintegrated: V15.2-inspired Inventory and Toxicity controls
- Runtime wiring: Inventory concentration -> risk multiplier; Toxicity -> pre-execution veto
- Added tests: `tests/test_v161_guards.py` and risk multiplier regression
- Default mode: dry

Validation results are recorded in `VALIDATION_STATUS.md`. Docker image build and Binance Testnet execution require external runtime capabilities / credentials and are not represented as completed by this local build.
