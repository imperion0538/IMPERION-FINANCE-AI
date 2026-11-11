#!/usr/bin/env python3
"""Capture public Binance USD-M aggTrade flow and report CVD/footprint (no API key, no orders)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from imperion.data.microstructure import TradeStore
from imperion.util import now_ms


def capture(symbol: str, seconds: int, ws_base: str = "wss://fstream.binance.com") -> dict:
    if seconds < 60 or seconds > 3600:
        raise ValueError("capture duration must be between 60 and 3600 seconds")
    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise RuntimeError("install requirements.txt (websocket-client) before capturing") from exc

    symbol = symbol.upper()
    store = TradeStore(whale_floor_usdt=1_000_000_000)
    started = time.time()
    errors: list[str] = []

    def on_open(ws):
        ws.send(json.dumps({"method": "SUBSCRIBE", "params": [f"{symbol.lower()}@aggTrade"], "id": 1}))

    def on_message(_ws, raw: str):
        try:
            payload = json.loads(raw)
            data: Any = payload.get("data", payload)
            if isinstance(data, dict) and data.get("e") == "aggTrade" and data.get("s") == symbol:
                store.add(symbol, int(data["T"]), float(data["p"]), float(data["q"]), bool(data["m"]))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return

    def on_error(_ws, error):
        errors.append(str(error)[:240])

    app = websocket.WebSocketApp(f"{ws_base}/market/stream", on_open=on_open,
                                 on_message=on_message, on_error=on_error)
    timer = threading.Timer(seconds, app.close)
    timer.daemon = True
    timer.start()
    try:
        app.run_forever(ping_interval=20, ping_timeout=10)
    finally:
        timer.cancel()
    elapsed = round(time.time() - started, 2)
    count = store.trade_count(symbol)
    window = min(61, max(2, seconds // 60 + 1))
    profile = store.flow_profile(symbol, window, now=now_ms(), limit=80)
    footprint = store.stacked_imbalances(symbol, window, now=now_ms())
    return {"symbol": symbol, "elapsed_seconds": elapsed, "aggtrade_events": count,
            "coverage_minutes": store.coverage_minutes(symbol), "flow_profile": profile,
            "footprint": footprint, "websocket_errors": errors,
            "method": "public Binance USD-M aggTrade; buyer-is-maker mapped to aggressor side; no orders sent"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--output", default="orderflow-capture.json")
    args = ap.parse_args()
    report = capture(args.symbol, args.seconds)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    if report["aggtrade_events"] == 0 or not report["flow_profile"].get("available"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
