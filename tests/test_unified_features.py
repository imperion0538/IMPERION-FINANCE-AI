from datetime import datetime, timezone, timedelta

from imperion.analysis.priceaction.sessions import find_sessions
from imperion.analysis.priceaction.types import Zone
from imperion.analysis.priceaction.zones import describe
from imperion.data.microstructure import TradeStore
from imperion.models import Candle


def test_mitigation_zone_has_safe_human_readable_description():
    zone = Zone(kind="MITIGATION", direction="BULLISH", lo=100.0, hi=101.0,
                created=12, quality=0.8)
    result = describe(zone)
    assert "mitigation" in result
    assert "bullish" in result


def test_hardened_trade_store_retains_cvd_delta_and_poc_profile():
    store = TradeStore(whale_floor_usdt=1)
    now = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    store.add("BTCUSDT", now, 100_000, 0.02, False)  # aggressive buy
    store.add("BTCUSDT", now + 1_000, 100_010, 0.01, True)  # aggressive sell
    profile = store.flow_profile("BTCUSDT", minutes=5, now=now + 2_000)
    assert profile["available"] is True
    assert profile["cvd"] == 0.01
    assert "delta_ratio" in profile and "poc_bin" in profile and profile["levels"]


def test_session_ranges_are_not_merged_across_utc_dates():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(72):
        dt = start + timedelta(hours=i)
        # Each UTC day has distinct prices; each session's highs/lows must stay
        # within that day's price band rather than mixing equal hours across days.
        day_offset = dt.day * 100
        hour = dt.hour
        price = day_offset + 100 + hour * 1.0
        candles.append(Candle(
            t=int(dt.timestamp() * 1000), o=price, h=price + 0.2,
            l=price - 0.2, c=price + 0.05, v=10, ct=int((dt + timedelta(minutes=59)).timestamp() * 1000)
        ))
    sessions = find_sessions(candles, atr=2.0)
    assert sessions
    for session in sessions:
        start_day = datetime.fromtimestamp(candles[session.start_i].t / 1000, tz=timezone.utc).date()
        end_day = datetime.fromtimestamp(candles[session.end_i].t / 1000, tz=timezone.utc).date()
        assert start_day == end_day
