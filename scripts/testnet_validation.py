#!/usr/bin/env python3
"""Thin wrapper: same as `python -m imperion testnet-check`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imperion.testnet import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
