#!/usr/bin/env python3
"""Offline release-bundle sanity checks; does not contact Binance or enable live trading."""
from __future__ import annotations
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from imperion.config import VERSION, load_settings

def main() -> int:
    checks = []
    checks.append(("VERSION metadata", (ROOT / "VERSION").read_text().strip() == VERSION == "14.3.0"))
    checks.append(("README exists", (ROOT / "README.md").is_file()))
    checks.append(("Dockerfile exists", (ROOT / "Dockerfile").is_file()))
    checks.append(("Railway config exists", (ROOT / "railway.json").is_file()))
    checks.append(("CI workflow exists", (ROOT / ".github/workflows/ci.yml").is_file()))
    checks.append(("Testnet workflow exists", (ROOT / ".github/workflows/testnet.yml").is_file()))
    checks.append(("Turkish production runbook exists", (ROOT / "docs/PRODUCTION_RUNBOOK_TR.md").is_file()))
    settings = load_settings({})
    checks.extend([
        ("default mode is dry", settings.mode == "dry"),
        ("default live confirmation empty", not settings.live_confirm),
        ("default risk <= 0.25%", settings.risk_per_trade_pct <= 0.25),
        ("default concurrent positions == 1", settings.max_concurrent_positions == 1),
        ("default leverage <= 2x", settings.leverage <= 2 and settings.max_leverage <= 2),
    ])
    for label, ok in checks:
        print(("PASS " if ok else "FAIL ") + label)
    print(f"BUNDLE_CHECK={'PASS' if all(ok for _, ok in checks) else 'FAIL'} ({sum(ok for _, ok in checks)}/{len(checks)})")
    return 0 if all(ok for _, ok in checks) else 1

if __name__ == "__main__":
    raise SystemExit(main())
