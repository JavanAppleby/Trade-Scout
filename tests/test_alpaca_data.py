"""scout.alpaca_data (0.3.2; SCOUT_SPEC 19.5, DESIGN 24): the requests are pinned to the market-data
host and the parsers read only what they need, refusing a changed shape with a short code. The
answers below follow Alpaca's documented shapes (market_alpaca_facts.md 1, 5, 6; the bars shape as
read every day by the owner's alpaca_bulk.fetch_bars). No network."""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scout.alpaca_data import (
    FREE_DELAY_S, SchemaError, closes_url, delayed_url, live_url, parse_closes, parse_minute_bars, parse_snapshots,
    parse_time,
)

SYMS = ("SPY", "QQQ", "IWM")
NOW = datetime(2026, 11, 11, 15, 2, 1, tzinfo=timezone.utc)  # Wednesday 10:02 ET


def snap(price: object = 681.2, t: str = "2026-11-11T15:01:58.412345678Z") -> dict:
    return {"latestTrade": {"t": t, "x": "V", "p": price, "s": 100, "c": ["@"], "i": 1, "z": "B"},
            "latestQuote": {"t": t, "ap": 681.3, "bp": 681.1, "as": 1, "bs": 2},
            "dailyBar": {"t": "2026-11-11T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "n": 1, "vw": 1},
            "prevDailyBar": {"t": "2026-11-10T05:00:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2, "n": 2, "vw": 2}}


def body(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


def test_parses_flat_and_wrapped_answers() -> None:
    flat = {"SPY": snap(681.2), "QQQ": snap(611.05), "IWM": snap(241)}
    for answer in (flat, {"snapshots": flat}):
        got = parse_snapshots(body(answer), SYMS)
        assert set(got) == set(SYMS)
        assert got["SPY"].text == "681.2" and got["QQQ"].text == "611.05" and got["IWM"].text == "241"
        assert got["SPY"].last == Decimal("681.2")
        assert got["SPY"].trade_at == datetime(2026, 11, 11, 15, 1, 58, 412345, tzinfo=timezone.utc)
    # the price is kept exactly as received (no float rounding)
    raw = b'{"SPY": {"latestTrade": {"t": "2026-11-11T15:01:58Z", "p": 681.10000000000001}}}'
    assert parse_snapshots(raw, ["SPY"])["SPY"].text == "681.10000000000001"


@pytest.mark.parametrize("text, micro", [
    ("2026-11-11T15:01:58.123456789Z", 123456), ("2026-11-11T15:01:58.12345678Z", 123456),
    ("2026-11-11T15:01:58.123456Z", 123456), ("2026-11-11T15:01:58.123Z", 123000), ("2026-11-11T15:01:58.1Z", 100000),
    ("2026-11-11T15:01:58Z", 0), ("2026-11-11T15:01:58.123456789+00:00", 123456), ("2026-11-11T10:01:58.5-05:00", 500000),
    ("2026-11-11t15:01:58z", 0),
])
def test_nine_digit_fractions_and_offsets(text: str, micro: int) -> None:
    got = parse_time(text)
    assert got == datetime(2026, 11, 11, 15, 1, 58, micro, tzinfo=timezone.utc) and got.tzinfo == timezone.utc


@pytest.mark.parametrize("bad", ["2026-11-11T15:01:58.1234567890Z", "2026-11-11T15:01:58", "2026-11-11 15:01:58Z",
                                 "2026-13-11T15:01:58Z", "", None, 1731337318, "yesterday"])
def test_unreadable_time_is_a_schema_error(bad: object) -> None:
    with pytest.raises(SchemaError, match="time"):
        parse_time(bad)


def test_null_empty_or_absent_symbol_is_missing() -> None:
    answer = {"SPY": snap(), "QQQ": None, "IWM": {}, "DIA": {"latestQuote": {"ap": 1}}}
    got = parse_snapshots(body(answer), ("SPY", "QQQ", "IWM", "DIA", "XLF"))
    assert set(got) == {"SPY"}
    assert parse_snapshots(body({"SPY": {"latestTrade": None}}), ["SPY"]) == {}


@pytest.mark.parametrize("raw, code", [
    (b"not json", "json"), (b"\xff\xfe{}", "encoding"), (b"", "empty"), (b"[]", "top"),
    (body({"SPY": snap("681.2")}), "price"), (body({"SPY": snap(True)}), "price"),
    (b'{"SPY": {"latestTrade": {"t": "2026-11-11T15:01:58Z", "p": NaN}}}', "number"),
    (b'{"SPY": {"latestTrade": {"t": "2026-11-11T15:01:58Z", "p": Infinity}}}', "number"),
    (body({"SPY": snap(0)}), "price"), (body({"SPY": snap(-1.5)}), "price"), (body({"SPY": snap(None)}), "price"),
    (body({"SPY": snap(t="bad")}), "time"), (body({"SPY": snap(t=None)}), "time"),
    (body({"SPY": [1, 2]}), "entry"), (body({"SPY": {"latestTrade": [1]}}), "trade"),
])
def test_changed_shape_is_a_schema_error(raw: bytes, code: str) -> None:
    with pytest.raises(SchemaError) as info:
        parse_snapshots(raw, ["SPY"])
    assert str(info.value) == code
    assert "681" not in str(info.value)  # a short code, never body text


@pytest.mark.parametrize("raw", [b"10000000.01", b"1e300", b"99999999999999999999999999999999", b"0.00009", b"1e-30"])
def test_an_absurd_price_is_a_changed_shape(raw: bytes) -> None:
    """Above 10,000,000 (or below the smallest US price step) a price is a changed shape, so no absurd
    number reaches the change's Decimal.quantize."""
    snapshot = b'{"SPY": {"latestTrade": {"t": "2026-11-11T15:01:58Z", "p": ' + raw + b"}}}"
    with pytest.raises(SchemaError, match="price"):
        parse_snapshots(snapshot, ["SPY"])
    with pytest.raises(SchemaError, match="price"):
        parse_minute_bars(b'{"bars": {"SPY": [{"t": "2026-11-11T14:45:00Z", "c": ' + raw + b"}]}}", ["SPY"])
    with pytest.raises(SchemaError, match="price"):
        parse_closes(b'{"bars": {"SPY": [{"t": "2026-11-10T05:00:00Z", "c": ' + raw + b"}]}}", ["SPY"],
                     date(2026, 11, 10))
    edge = b'{"SPY": {"latestTrade": {"t": "2026-11-11T15:01:58Z", "p": 10000000}}}'
    assert parse_snapshots(edge, ["SPY"])["SPY"].text == "10000000"
    assert parse_snapshots(edge.replace(b"10000000", b"0.0001"), ["SPY"])["SPY"].text == "0.0001"


def _q(url: str) -> dict[str, str]:
    parts = urllib.parse.urlsplit(url)
    assert parts.scheme == "https" and parts.hostname == "data.alpaca.markets" and parts.port is None
    assert not parts.username and not parts.password and not parts.fragment
    return dict(urllib.parse.parse_qsl(parts.query))


def test_url_is_pinned_https_data_host() -> None:
    live = live_url(SYMS)
    assert urllib.parse.urlsplit(live).path == "/v2/stocks/snapshots"
    assert _q(live) == {"symbols": "SPY,QQQ,IWM", "feed": "iex"}
    d = _q(delayed_url(SYMS, NOW))
    end = NOW - timedelta(seconds=FREE_DELAY_S)
    assert d["end"] == end.strftime("%Y-%m-%dT%H:%M:%SZ") and d["timeframe"] == "1Min" and d["feed"] == "sip"
    assert d["start"] == (end - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ") and d["adjustment"] == "raw"
    c = _q(closes_url(SYMS, date(2026, 11, 10), date(2026, 11, 11)))
    assert c["timeframe"] == "1Day" and c["start"] == "2026-11-03T00:00:00Z" and c["adjustment"] == "split"
    assert c["end"] == "2026-11-11T05:00:00Z" and c["feed"] == "sip"  # 00:00 ET of the trading day
    for url in (live, delayed_url(SYMS, NOW), closes_url(SYMS, date(2026, 11, 10), date(2026, 11, 11))):
        assert "/v2/orders" not in url and "/v2/account" not in url and "/v2/positions" not in url
    with pytest.raises(ValueError):
        live_url(["SPY&feed=sip"])
    with pytest.raises(ValueError):
        live_url([])


def test_url_text_is_plain_urlencode_with_a_20_minute_delay() -> None:
    """The exact request text: plain `urlencode` (commas and colons percent-encoded), the form the owner's
    probe and downloader ran against the real service; whole-tape minute bars ending 20 minutes ago."""
    assert FREE_DELAY_S == 20 * 60
    assert live_url(SYMS) == "https://data.alpaca.markets/v2/stocks/snapshots?symbols=SPY%2CQQQ%2CIWM&feed=iex"
    assert delayed_url(SYMS, NOW) == (
        "https://data.alpaca.markets/v2/stocks/bars?symbols=SPY%2CQQQ%2CIWM&timeframe=1Min"
        "&start=2026-11-11T14%3A27%3A01Z&end=2026-11-11T14%3A42%3A01Z&feed=sip&adjustment=raw&limit=1000")
    assert closes_url(SYMS, date(2026, 11, 10), date(2026, 11, 11)) == (
        "https://data.alpaca.markets/v2/stocks/bars?symbols=SPY%2CQQQ%2CIWM&timeframe=1Day"
        "&start=2026-11-03T00%3A00%3A00Z&end=2026-11-11T05%3A00%3A00Z&feed=sip&adjustment=split&limit=1000")
    # summer: 00:00 EDT is 04:00 UTC; a Monday's request ends at Monday 00:00 ET (Friday's bar is before it)
    assert _q(closes_url(SYMS, date(2026, 8, 14), date(2026, 8, 17)))["end"] == "2026-08-17T04:00:00Z"


def bars(rows: dict, token: object = None) -> bytes:
    return body({"bars": rows, "next_page_token": token})


def bar(t: str, c: object) -> dict:
    return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 100, "n": 3, "vw": c}


def test_minute_bars_take_the_newest_bar_at_the_end_of_its_minute() -> None:
    raw = bars({"SPY": [bar("2026-11-11T14:44:00Z", 680.5), bar("2026-11-11T14:45:00Z", 680.75)],
                "QQQ": [bar("2026-11-11T14:45:00Z", 610)], "IWM": []})
    got = parse_minute_bars(raw, SYMS)
    assert set(got) == {"SPY", "QQQ"}  # no bar: missing
    assert got["SPY"].text == "680.75" and got["SPY"].trade_at == datetime(2026, 11, 11, 14, 46, tzinfo=timezone.utc)
    assert parse_minute_bars(bars(None), SYMS) == {} and parse_minute_bars(bars({}), SYMS) == {}


@pytest.mark.parametrize("raw, code", [
    (bars({"SPY": [bar("2026-11-11T14:45:00Z", 1)]}, token="abc"), "paged"),
    (body({"next_page_token": None}), "bars"), (body([]), "top"),
    (bars({"SPY": {"t": "x"}}), "rows"), (bars({"SPY": [5]}), "row"),
    (bars({"SPY": [bar("2026-11-11T14:45:00Z", "1.0")]}), "price"),
    (bars({"SPY": [bar("bad", 1)]}), "time"),
])
def test_bars_changed_shape_is_a_schema_error(raw: bytes, code: str) -> None:
    with pytest.raises(SchemaError) as info:
        parse_minute_bars(raw, ["SPY"])
    assert str(info.value) == code


def test_closes_take_the_bar_dated_the_previous_day_in_et() -> None:
    raw = bars({
        "SPY": [bar("2026-11-06T05:00:00Z", 670), bar("2026-11-09T05:00:00Z", 675), bar("2026-11-10T05:00:00Z", 679.1)],
        "QQQ": [bar("2026-11-09T05:00:00Z", 600)],  # no bar on the 10th: missing
        "IWM": [bar("2026-11-10T04:00:00Z", 241.65)],  # 23:00 ET on the 9th in winter: not the 10th
    })
    got = parse_closes(raw, SYMS, date(2026, 11, 10))
    assert got == {"SPY": Decimal("679.1")}
    summer = bars({"SPY": [bar("2026-08-14T04:00:00Z", 640.27)]})
    assert parse_closes(summer, ["SPY"], date(2026, 8, 14)) == {"SPY": Decimal("640.27")}
