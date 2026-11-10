"""Alpaca's market-data answers: every assumption Scout makes about Alpaca lives here (0.3.2, the
market feed for the reads; SCOUT_SPEC 19.5, DESIGN 24).

Scout asks only `https://data.alpaca.markets` (pinned; never an order, account or position address)
with the owner's free key, three ways (build plan amendment A; checked against Alpaca's docs, its own
SDKs and its staff's answers, `market_alpaca_facts.md`):

- live: `GET /v2/stocks/snapshots?symbols=...&feed=iex`. The free plan's live prices come from the
  IEX exchange only, which trades from about 08:00 to 17:00 ET; outside those hours the last IEX
  trade is hours old. Only `latestTrade.t` and `latestTrade.p` are read (`dailyBar` and
  `prevDailyBar` are IEX-only prices on this feed and are ignored). The answer is an object keyed by
  symbol at the top level (a `snapshots` wrapper is tolerated).
- delayed: `GET /v2/stocks/bars` with 1-minute bars of the whole tape (`feed=sip`) ending
  `FREE_DELAY_S` (20 minutes: the form the owner's key was proven to allow on 10 Nov 2026) before now,
  over the 15 minutes before that. The newest bar per symbol gives `last = c` at the end of its
  minute (`t` + 60 s).
- closes: `GET /v2/stocks/bars` with daily bars of the whole tape over the week before, ending at
  00:00 ET of the trading day (always hours old, so never "recent" data); the bar whose `t` (midnight
  New York, in UTC) falls on the previous trading day in ET gives that day's close.

Query strings are built with plain `urllib.parse.urlencode` (commas and colons percent-encoded), the
form already run against the real service.

Every function here is pure. A parser raises `SchemaError(code)` with a short code and never body
text; a symbol with no data is simply absent from the result. Prices are kept as the text received
(`Decimal`) and must lie from 0.0001 to 10,000,000 (else `SchemaError("price")`); times accept 0 to 9
fractional digits and `Z` or an offset.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from scout.clock import et_datetime, to_et
from scout.config_reads import DATA_HOST

__all__ = ["BARS_PATH", "CLOSES_LOOKBACK_DAYS", "DATA_HOST", "DELAYED_WINDOW_S", "FREE_DELAY_S", "SNAPSHOTS_PATH",
           "Price", "SchemaError", "closes_url", "delayed_url", "live_url", "parse_closes", "parse_minute_bars",
           "parse_snapshots", "parse_time"]

SNAPSHOTS_PATH = "/v2/stocks/snapshots"
BARS_PATH = "/v2/stocks/bars"
FREE_DELAY_S = 20 * 60  # the free plan serves whole-tape data at least 15 minutes old; 20 is the form proven with the key
DELAYED_WINDOW_S = 15 * 60  # the delayed request covers the 15 minutes before that
CLOSES_LOOKBACK_DAYS = 7
BARS_LIMIT = 1000  # 3 symbols x 15 one-minute bars, or x 5 daily bars, fit in one page
MINUTE = timedelta(seconds=60)
MAX_PRICE = Decimal(10_000_000)  # above this a price is a changed shape
MIN_PRICE = Decimal("0.0001")  # the smallest US price increment; below it (but above 0) a changed shape too
_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:\d{2})")


class SchemaError(ValueError):
    """Alpaca's answer did not have the expected shape. The message is a short code, never body text."""


@dataclass(frozen=True)
class Price:
    """One symbol's price as read from an answer: `last` (exact, as received) at `trade_at` (UTC)."""

    symbol: str
    last: Decimal
    trade_at: datetime

    @property
    def text(self) -> str:
        return format(self.last, "f")


# --------------------------------------------------------------------------- the requests


def _url(path: str, params: Mapping[str, str]) -> str:
    """Plain `urlencode` (commas and colons percent-encoded): the form the owner's probe and downloader
    have run against the real service."""
    return f"https://{DATA_HOST}{path}?" + urllib.parse.urlencode(params)


def _stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _symbols(symbols: Iterable[str]) -> str:
    out = [str(s).strip().upper() for s in symbols]
    if not out or not all(re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", s) for s in out):
        raise ValueError("symbols must be tickers")
    return ",".join(out)


def live_url(symbols: Iterable[str]) -> str:
    """The live snapshot request (IEX prices)."""
    return _url(SNAPSHOTS_PATH, {"symbols": _symbols(symbols), "feed": "iex"})


def delayed_url(symbols: Iterable[str], now: datetime) -> str:
    """The whole-tape 1-minute bars ending `FREE_DELAY_S` before `now`."""
    end = now - timedelta(seconds=FREE_DELAY_S)
    return _url(BARS_PATH, {"symbols": _symbols(symbols), "timeframe": "1Min",
                            "start": _stamp(end - timedelta(seconds=DELAYED_WINDOW_S)), "end": _stamp(end),
                            "feed": "sip", "adjustment": "raw", "limit": str(BARS_LIMIT)})


def closes_url(symbols: Iterable[str], prev_day: date, day: date) -> str:
    """The whole-tape daily bars over the week before `prev_day`, ending at 00:00 ET of the trading day
    `day`: only bars up to `prev_day` are needed, and an end at least four hours old (polls start at
    04:00 ET) can never meet Alpaca's rule against recent whole-tape data."""
    start = datetime(prev_day.year, prev_day.month, prev_day.day, tzinfo=timezone.utc) - timedelta(
        days=CLOSES_LOOKBACK_DAYS)
    return _url(BARS_PATH, {"symbols": _symbols(symbols), "timeframe": "1Day", "start": _stamp(start),
                            "end": _stamp(et_datetime(day, time(0, 0))), "feed": "sip",
                            "adjustment": "split", "limit": str(BARS_LIMIT)})


# --------------------------------------------------------------------------- the answers


def parse_time(text: Any) -> datetime:
    """An RFC 3339 time with 0 to 9 fractional digits and `Z` or an offset, as aware UTC. The fraction
    is cut to 6 digits before `fromisoformat`, so Python 3.11 to 3.13 read it alike."""
    if not isinstance(text, str):
        raise SchemaError("time")
    m = _TIME.fullmatch(text.strip())
    if m is None:
        raise SchemaError("time")
    day, clock, frac, zone = m.groups()
    frac = ((frac or "") + "000000")[:6]
    zone = "+00:00" if zone in ("Z", "z") else zone
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{frac}{zone}").astimezone(timezone.utc)
    except ValueError:
        raise SchemaError("time") from None


def _reject_constant(_name: str) -> Any:
    raise SchemaError("number")


def _load(body: bytes | None) -> Any:
    if not body:
        raise SchemaError("empty")
    try:
        text = bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        raise SchemaError("encoding") from None
    try:
        return json.loads(text, parse_float=Decimal, parse_int=Decimal, parse_constant=_reject_constant)
    except SchemaError:
        raise
    except (ValueError, RecursionError):
        raise SchemaError("json") from None


def _price(value: Any) -> Decimal:
    """A JSON number (not a string, not a bool), finite, from `MIN_PRICE` to `MAX_PRICE`. Anything else is
    a changed shape, so no absurd number ever reaches the change's `Decimal.quantize`."""
    if not isinstance(value, Decimal) or not value.is_finite() or not MIN_PRICE <= value <= MAX_PRICE:
        raise SchemaError("price")
    return value


def _object(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(code)
    return value


def parse_snapshots(body: bytes | None, symbols: Iterable[str]) -> dict[str, Price]:
    """symbol -> Price from a snapshots answer. A null or `{}` entry, an entry without `latestTrade`,
    or no entry means that symbol is missing (absent from the result)."""
    top = _object(_load(body), "top")
    if isinstance(top.get("snapshots"), dict):  # tolerated wrapper (symbols are upper case, so no clash)
        top = top["snapshots"]
    out: dict[str, Price] = {}
    for sym in symbols:
        entry = top.get(sym)
        if entry is None or entry == {}:
            continue
        trade = _object(entry, "entry").get("latestTrade")
        if trade is None or trade == {}:
            continue
        trade = _object(trade, "trade")
        out[sym] = Price(sym, _price(trade.get("p")), parse_time(trade.get("t")))
    return out


def _bars(body: bytes | None) -> Mapping[str, Any]:
    top = _object(_load(body), "top")
    if "bars" not in top:
        raise SchemaError("bars")
    if top.get("next_page_token") is not None:
        raise SchemaError("paged")  # three symbols never fill a page: a changed shape
    bars = top.get("bars")
    return {} if bars is None else _object(bars, "bars")


def _rows(bars: Mapping[str, Any], sym: str) -> list[tuple[datetime, Decimal]]:
    rows = bars.get(sym)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise SchemaError("rows")
    return [(parse_time(r.get("t")), _price(r.get("c"))) for r in (_object(x, "row") for x in rows)]


def parse_minute_bars(body: bytes | None, symbols: Iterable[str]) -> dict[str, Price]:
    """symbol -> Price from a 1-minute bars answer: the bar with the newest `t`; `last = c` at the end
    of that minute. A symbol with no bar is missing."""
    bars = _bars(body)
    out: dict[str, Price] = {}
    for sym in symbols:
        rows = _rows(bars, sym)
        if rows:
            at, close = max(rows, key=lambda r: r[0])
            out[sym] = Price(sym, close, at + MINUTE)
    return out


def parse_closes(body: bytes | None, symbols: Iterable[str], prev_day: date) -> dict[str, Decimal]:
    """symbol -> the close of the daily bar whose `t` falls on `prev_day` in ET. A symbol without that
    bar is missing."""
    bars = _bars(body)
    out: dict[str, Decimal] = {}
    for sym in symbols:
        for at, close in _rows(bars, sym):
            if to_et(at).date() == prev_day:
                out[sym] = close
    return out
