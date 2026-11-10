"""`python -m scout reads market` (`Check Market.bat`): check the market feed now (0.3.2; build plan
amendment E). It says, in this order:

1. whether Alpaca answered, request by request;
2. the newest prices each request returned, with their age and where they came from, even when the
   feed would not use them because they are too old (in plain words, never reason codes);
3. what a reader would see right now (from the running Scout's records, as a read would);
4. one closing line: whether the key works.

It never prints the key and writes no record or state, only its log lines: its fetcher keeps no
state file, and the poll writes no market or miss line and no raw file. Exit codes: 0 the key works
(a request was answered and the answer could be read, even when the prices are only old, as in the
early hours); 1 no request was answered, or no answer could be read; 2 not set up (the feed switched
off, no contact_email, or no key).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from scout.calendar import session_at
from scout.clock import to_et
from scout.http import HttpFetcher
from scout.market_feed import (
    CLOSES, DELAYED, LIVE, POLL_SESSIONS, PRICE_LEGS, MarketPoller, PollOutcome, pre_market, stamp_age,
)
from scout.reads_market import latest_record, market_line, shown_market
from scout.secrets import ALPACA_ID_NAMES, ALPACA_SECRET_NAMES, alpaca_key

__all__ = ["exit_code", "report", "run_check"]

_NAMES = {LIVE: "Live prices (IEX exchange only)", DELAYED: "Whole-market prices (at least 20 minutes old)",
          CLOSES: "Previous closes (whole market"}
_SOURCE = {LIVE: "live IEX prices", DELAYED: "whole-market prices, delayed"}
_CODES = {"timeout": "no answer in time", "network": "could not connect", "backoff": "not asked: Alpaca asked Scout "
          "to wait (it answered 403 or 429 a moment ago)", "dropped": "not asked again today (Alpaca refused it while "
          "it answered the others)", "skipped": "not asked (an earlier request got no answer, so the check stopped)",
          "schema": "answered, but not in the shape Scout knows", "too_large": "answered with too much data",
          "host_cap": "not asked: today's request limit is reached", "no_http": "not asked: contact_email is empty",
          "refused": "not asked: the feed is off", "no_prev_close": "answered, without the previous day's bar"}
# why the feed would not use a request's prices, in plain words (never the reason codes)
_WHY = {"old": "they are from before today's 04:00 ET start",
        "no_recent_bar": "Alpaca had no whole-market 1-minute bar for every symbol 20 to 35 minutes ago",
        "missing_symbol": "not every symbol had a price", "no_prev_close": "not every symbol had a previous close",
        "future_time": "a trade time is over a minute ahead of this PC's clock (check the PC's clock)"}
UNREADABLE = frozenset({"schema", "too_large", "network"})  # codes of a 200 answer that could not be read
REFUSED_ONE = (403, 422)  # the statuses that make the feed leave one request out until the next day


def _why(why: str, now: datetime) -> str:
    """Why the feed would not use a request's prices, in plain words for the hour it is."""
    text = _WHY[why]
    if why == "no_recent_bar" and pre_market(now):
        return text + " (few trades happen early in the morning)"
    if why == "no_recent_bar" and session_at(now) not in POLL_SESSIONS:
        return text + " (the market is closed)"
    return text


def _leg_text(code: str, status: int | None, refused_key: bool) -> str:
    """One request's answer. "The key was refused" only when every answer was a refusal: a single refused
    request beside an answered one is that request's own, and the key works."""
    if code == "ok":
        return "answered"
    if code.startswith("http_"):
        if status in (401, 403) and refused_key:
            return f"answered HTTP {status} (the key was refused)"
        who = " (Alpaca refused this request)" if status in (401, *REFUSED_ONE) else ""
        return f"answered HTTP {status}{who}"
    return _CODES.get(code, "no usable answer")


def _refused_line(out: PollOutcome) -> str | None:
    """A request refused while another was answered: say that the key itself works and what Scout does."""
    if out.reason == "refused_key" or not any(leg.status == 200 for leg in out.legs.values()):
        return None
    names = [_NAMES[n].split(" (")[0].lower() for n in (LIVE, DELAYED, CLOSES)
             if n in out.legs and out.legs[n].status in REFUSED_ONE]
    if not names:
        return None
    return (f"Alpaca refused the request for {' and '.join(names)} but answered another, so the key itself works. "
            "The running Scout leaves a refused request out until the next day and carries on with the rest.")


def _pct(last: Decimal, prev: Decimal | None) -> str:
    try:
        return "" if prev is None else f" ({float((last / prev - 1) * 100):+.2f}%)"
    except (InvalidOperation, ArithmeticError, ValueError, OverflowError):
        return ""


def _prices_line(out: PollOutcome, name: str, now: datetime, symbols: tuple[str, ...]) -> str | None:
    """The newest prices one request returned, with their age, whether or not the feed would use them."""
    prices = out.legs[name].prices if name in out.legs else {}
    why = out.problems.get(name)
    if not prices:
        return f"{_NAMES[name].split(' (')[0]}: no prices, because {_why(why, now)}." if why in _WHY else None
    at = min(p.trade_at for p in prices.values())
    parts = [f"{s} {prices[s].text}{_pct(prices[s].last, out.closes.get(s))}" if s in prices else f"{s} no price"
             for s in (*symbols, *(s for s in prices if s not in symbols))]
    line = (f"Newest prices: {'; '.join(parts)}, at {to_et(at):%H:%M:%S} ET on {to_et(at):%Y-%m-%d}, "
            f"{stamp_age(at, now)} old ({_SOURCE[name]}); change from the whole market's close on "
            f"{out.prev_day.isoformat()}.")
    return line + (f" Not used now: {_why(why, now)}." if why in _WHY else "")


def _state_line(out: PollOutcome, cfg: Any, now: datetime) -> str | None:
    newest = [min(p.trade_at for p in out.legs[n].prices.values()) for n in PRICE_LEGS
              if n in out.legs and out.legs[n].prices]
    if session_at(now) not in POLL_SESSIONS:
        old = bool(newest) and (now - max(newest)).total_seconds() >= 3600
        return (("The market is closed now, so these prices are hours old; the key works all the same. " if old
                 else "The market is closed now. ") + "Scout polls only from 04:00 to 20:00 ET on trading days.")
    if out.state == "stale":
        early = " This is normal before about 08:00 ET." if pre_market(now) else ""
        return (f"Prices this old (over {int(cfg.reads.market_max_age_s // 60)} minutes, reads_market_max_age_s, or "
                f"from before today's 04:00 ET) are not shown to a reader.{early}")
    if out.state == "failed" and out.reason != "refused_key":
        return "Scout's feed would record no prices from this poll (see the lines above)."
    return None


def exit_code(out: PollOutcome) -> int:
    """0 when a request was answered and its answer could be read (the key works), else 1."""
    readable = any(leg.status == 200 and leg.code not in UNREADABLE for leg in out.legs.values())
    return 0 if readable else 1


def _closing(out: PollOutcome) -> str:
    if out.reason == "refused_key":
        return "Alpaca refused the key: check the two lines in Scout's .env."
    unreadable = [leg for leg in out.legs.values() if leg.status == 200 and leg.code in UNREADABLE]
    if exit_code(out) == 0:
        works = "The key works: Alpaca accepted it and answered."
        if unreadable:
            return works + " One answer was not in the shape Scout knows: copy these lines to Claude."
        if out.state == "failed":  # the key is fine, yet this poll would record nothing
            return works + (" If the check still says in a few minutes that no prices would be recorded, copy these "
                            "lines to Claude.")
        return works
    if unreadable:
        return ("Alpaca answered, but not in the shape Scout knows, so the key could not be confirmed: copy these "
                "lines to Claude.")
    return "Alpaca gave no answer this time, so the key could not be checked: try again in a minute."


def report(out: PollOutcome, cfg: Any, paths: Any) -> list[str]:
    """The check's lines (pure apart from reading the market file)."""
    now = out.checked_at
    lines = ["Alpaca market data (data.alpaca.markets), with the key in Scout's .env:"]
    for name in (LIVE, DELAYED, CLOSES):
        leg = out.legs.get(name)
        if leg is None:
            continue
        extra = f", {out.prev_day.isoformat()})" if name == CLOSES else ""
        lines.append(f"- {_NAMES[name]}{extra}: {_leg_text(leg.code, leg.status, out.reason == 'refused_key')}.")
    refused = _refused_line(out)
    lines += [refused] if refused else []
    if now is not None:
        syms = tuple(cfg.reads.market_symbols)
        lines += [line for line in (_prices_line(out, n, now, syms) for n in PRICE_LEGS) if line]
        state = _state_line(out, cfg, now)
        lines += [state] if state else []
    shown = shown_market(None, latest_record(paths, now), now, cfg.reads.market_max_age_s,
                         cfg.reads.market_symbols) if now is not None else None
    seen = market_line(shown, now, to_et(now).date()) if now is not None else "Market: not available"
    lines.append(f"A reader now would see: {seen}" + ("" if shown else " (the running Scout has recorded no market "
                                                      "prices recent enough)"))
    if not cfg.reads.enabled:
        lines.append("Reads are off (reads_enabled = false): no reader runs, and Scout records no market prices.")
    lines.append(_closing(out))
    return lines


def run_check(ctx: Any, args: Any) -> int:
    cfg = ctx.cfg
    if not cfg.reads.market_enabled:
        print("The market feed is off (reads_market_enabled = false in config.toml).")
        return 2
    if not cfg.http_allowed():
        print("contact_email is empty in config.toml: Scout makes no web requests at all until it is set.")
        return 2
    key = alpaca_key()
    if key is None:
        print("No Alpaca key: add two lines to Scout's .env, the key id as one of "
              f"{', '.join(ALPACA_ID_NAMES)} and the secret as one of {', '.join(ALPACA_SECRET_NAMES)} (README).")
        return 2
    fetcher = HttpFetcher(cfg, ctx.clock, log=ctx.log.getChild("market.check.http"))  # no state file
    poller = MarketPoller(cfg, ctx.paths, fetcher, key, ctx.clock, raw=None, log=ctx.log.getChild("market.check"),
                          write=False)
    out = poller.poll(ctx.clock.now())
    for line in report(out, cfg, ctx.paths):
        print(line)
    return exit_code(out)
