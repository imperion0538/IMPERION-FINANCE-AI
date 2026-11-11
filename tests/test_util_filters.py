from datetime import datetime, timezone
from decimal import Decimal

from imperion.exchange.filters import SymbolFilters, fmt, parse_exchange_info
from imperion.util import candle_open_for, fnum, last_closed_open, tf_ms, get_tz


def ms(y, m, d, h=0, mi=0, s=0):
    return int(datetime(y, m, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


def test_weekly_candle_opens_on_monday():
    t = ms(2026, 11, 11, 13, 30)                 # a Wednesday
    opened = datetime.fromtimestamp(candle_open_for(t, "1w") / 1000, tz=timezone.utc)
    assert opened.weekday() == 0 and opened.hour == 0 and opened.day == 9
    assert last_closed_open(t, "1w") == candle_open_for(t, "1w") - tf_ms("1w")


def test_intraday_boundaries():
    t = ms(2026, 11, 10, 10, 7, 30)
    assert candle_open_for(t, "5m") == ms(2026, 11, 10, 10, 5)
    assert candle_open_for(t, "4h") == ms(2026, 11, 10, 8)
    assert last_closed_open(t, "1m") == ms(2026, 11, 10, 10, 6)


def test_fnum_rejects_garbage():
    assert fnum("1.5") == 1.5 and fnum(None, 7) == 7 and fnum(float("nan"), 0) == 0 and fnum(True, 3) == 3 and fnum("x") is None


def test_istanbul_is_utc_plus_3():
    tz = get_tz("Europe/Istanbul")
    assert datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc).astimezone(tz).hour == 8


def test_filters_floor_and_round():
    f = SymbolFilters("X", tick_size=Decimal("0.01"), step_size=Decimal("0.1"), market_step=Decimal("0.1"), min_notional=Decimal("5"))
    assert f.floor_qty(1.2999) == Decimal("1.2")
    assert f.round_price(10.006, "down") == Decimal("10.00") and f.round_price(10.001, "up") == Decimal("10.01")
    assert fmt(Decimal("1.2300")) == "1.23" and fmt(Decimal("1E+2")) == "100" and fmt(0.1 + 0.2) == "0.30000000000000004"
    assert f.min_qty_for_notional(100) * 100 >= 5


def test_parse_exchange_info():
    data = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"}, {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"}, {"filterType": "MIN_NOTIONAL", "notional": "100"}]}]}
    f = parse_exchange_info(data)["BTCUSDT"]
    assert f.tick_size == Decimal("0.10") and f.min_notional == Decimal("100") and f.market_max_qty == Decimal("120")
