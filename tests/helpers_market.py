"""A fake Alpaca market-data service for the market feed's tests (0.3.2). No network: it is the
fetcher's transport. The answers follow Alpaca's documented shapes (snapshots: symbols at the top
level; bars: `{"bars": {SYM: [...]}, "next_page_token": null}`). The key values are made up."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from helpers_fakes import FakeResponse

from scout.clock import ManualClock
from scout.http import HttpFetcher
from scout.market_feed import CLOSES, DELAYED, LIVE, MarketPoller
from scout.secrets import AlpacaKey

FAKE_ID = "made-up-key-id-for-tests"  # made up and not shaped like a real key, so no scanner mistakes it
FAKE_SECRET = "made-up-secret-for-tests-0123456789"
KEY = AlpacaKey(FAKE_ID, FAKE_SECRET)
SYMS = ("SPY", "QQQ", "IWM")
LAST = {"SPY": 681.2, "QQQ": 611.05, "IWM": 241.13}
CLOSE = {"SPY": "#679.10", "QQQ": 608.49, "IWM": 241.65}  # "#679.10": the JSON number 679.10, written as is


def z(at: datetime) -> str:
    """Alpaca's 9-digit fraction (the last 3 digits are cut off by Scout)."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "789Z"


def ok(obj: Any) -> FakeResponse:
    """A 200 answer; a string "#<number>" becomes that JSON number exactly as written."""
    text = re.sub(r'"#(-?[0-9.]+)"', lambda m: m.group(1), json.dumps(obj))
    return FakeResponse(200, {"content-type": "application/json"}, text.encode("utf-8"))


def live_answer(at: datetime, prices: Mapping[str, Any] = LAST, *, skip: tuple[str, ...] = ()) -> FakeResponse:
    return ok({s: {"latestTrade": {"t": z(at - timedelta(seconds=i)), "p": p, "x": "V", "s": 100},
                   "dailyBar": {"t": "2026-01-01T05:00:00Z", "c": 1}, "prevDailyBar": {"t": "2026-01-01T05:00:00Z", "c": 2}}
               for i, (s, p) in enumerate(prices.items()) if s not in skip})


def delayed_answer(end: datetime, prices: Mapping[str, Any] = LAST, *, skip: tuple[str, ...] = ()) -> FakeResponse:
    start = end - timedelta(minutes=1)
    return ok({"bars": {s: [{"t": z(start - timedelta(minutes=1)), "o": p, "h": p, "l": p, "c": 1.0, "v": 9, "n": 1,
                             "vw": p},
                            {"t": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": p, "h": p, "l": p, "c": p, "v": 9, "n": 1,
                             "vw": p}]
                        for s, p in prices.items() if s not in skip}, "next_page_token": None})


def closes_answer(day: date, closes: Mapping[str, Any] = CLOSE) -> FakeResponse:
    t = datetime(day.year, day.month, day.day, 5, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # EST midnight
    older = (datetime(day.year, day.month, day.day, 5, tzinfo=timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return ok({"bars": {s: [{"t": older, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "n": 1, "vw": 1},
                            {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 1, "n": 1, "vw": c}]
                        for s, c in closes.items()}, "next_page_token": None})


def asked_end(url: str) -> datetime:
    """The `end` the request itself asked for (the delayed answer is built from it, never from a fixed delay)."""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    return datetime.strptime(q["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def leg_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    if parts.path == "/v2/stocks/snapshots":
        return LIVE
    return DELAYED if q.get("timeframe") == "1Min" else CLOSES


Route = Any  # a FakeResponse, an exception, or a callable(url, now) -> FakeResponse


class FakeAlpaca:
    """The fetcher's transport. `routes[leg]` answers each request; every call is kept."""

    def __init__(self, clock: ManualClock, prev_day: date | None = None, **routes: Route) -> None:
        self.clock = clock
        self.prev_day = prev_day
        self.routes: dict[str, Route] = {
            LIVE: lambda url, now: live_answer(now - timedelta(seconds=2)),
            DELAYED: lambda url, now: delayed_answer(asked_end(url)),
            CLOSES: lambda url, now: closes_answer(self.prev_day or date(2026, 11, 10)),
        }
        self.routes.update({k: v for k, v in routes.items()})
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout_s: float, max_bytes: int) -> FakeResponse:
        leg = leg_of(url)
        self.calls.append((leg, url, dict(headers), timeout_s))
        route = self.routes[leg]
        if isinstance(route, BaseException):
            raise route
        if callable(route) and not isinstance(route, FakeResponse):
            return route(url, self.clock.now())
        return route

    def legs(self) -> list[str]:
        return [c[0] for c in self.calls]


def poller(cfg: Any, paths: Any, clock: ManualClock, fake: Callable[..., Any], *, raw: Any = None,
           log: Any = None, key: Any = KEY, write: bool = True) -> MarketPoller:
    fetcher = HttpFetcher(cfg, clock, transport=fake, log=log)
    return MarketPoller(cfg, paths, fetcher, key, clock, raw=raw, log=log, write=write)
