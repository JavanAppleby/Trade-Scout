"""The market feed for the thesis reads (0.3.2; owner, 2026-11-11: "find another source ... like
Alpaca"; SCOUT_SPEC 19.5, DESIGN 24).

`MarketPoller.tick(now)` runs in Scout's loop every `reads_market_poll_s` (60 s), in the PRE, CORE and
POST sessions only, and only with `reads_enabled`, `reads_market_enabled` and the owner's Alpaca key
(`scout.jobs_market`). One poll asks Alpaca's market-data service (`scout.alpaca_data`; the host
`data.alpaca.markets` only) up to three things, with the key's two headers and the plain User-Agent
`Scout/<version>` (never the contact email):

1. live (`iex`): the IEX exchange's last trade per symbol (real time; IEX trades about 08:00-17:00 ET);
2. delayed (`sip_delayed`): the whole market's last 1-minute bar ending 20 minutes before now;
3. closes: the whole market's close on the previous trading day, until the day's closes are held.

Each price leg gives one candidate when every configured symbol has a price above 0, no later than
60 s ahead of Scout's clock, no earlier than the day's 04:00 ET, and a previous close. The newest
candidate (`at` = its oldest trade) wins; a tie goes to the delayed one. A record is appended to
`state/reads/market/<ET date>.jsonl` only when it is at most `reads_market_max_age_s` old (else the
poll is `stale`, or `failed` when the other price request failed outright). A poll that sent a
request and recorded nothing appends one `miss` line with a reason code (never body text). Prices
are stored as the text received; `change_pct` is a fraction to 6 places (`last / prev_close - 1`).

Nothing here raises into the scheduler. The fetch is quiet (its per-request line at DEBUG); the
poller logs once per change of state. A timeout or network failure on any request ends that poll:
no further request is sent in it. A 403 or 429 puts the host in the fetcher's backoff (no request
while it lasts; not a failure). A leg answered 403 or 422 while another request of the same poll was
answered 200 is dropped until the next ET day (the plan refused for that request, not the key). When
no leg answers 200 and one answers 401 or 403, Alpaca refused the key. After 3 failed polls in a row
the interval doubles (up to `source_backoff_max_min`; the exponent is capped); a poll with usable or
merely old data resets it.

The feed is evidence for the reads only (build plan amendment B): it has no health record, no board
entry, no params line, and its fetch source is not in `cfg.sources`, so no answer of Scout's can
change with it. `python -m scout reads market` (`Check Market.bat`) runs one poll and writes no
record or state, only its log lines.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from scout import __version__
from scout.alpaca_data import (
    Price, SchemaError, closes_url, delayed_url, live_url, parse_closes, parse_minute_bars, parse_snapshots,
)
from scout.calendar import Session, is_trading_day, pre_open, previous_trading_day, session_at
from scout.clock import Clock, et_date, parse_iso
from scout.config_reads import MARKET_SOURCE
from scout.reads_file import iso_us
from scout.reads_market import market_file
from scout.store import append_jsonl

__all__ = ["DELAYED", "FAILS_BEFORE_SLOWING", "LIVE", "MarketPoller", "PRICE_LEGS", "PollOutcome", "SOURCE",
           "candidate", "choose", "cli_reads_market", "market_day", "market_record", "miss_line", "pre_market"]

SOURCE = MARKET_SOURCE
LIVE, DELAYED, CLOSES = "iex", "sip_delayed", "closes"
PRICE_LEGS = (LIVE, DELAYED)
PREV_CLOSE_FEED = "sip"
FORMAT = 1
FUTURE_TOLERANCE_S = 60  # a PC clock a little behind Alpaca's must not hide the live prices
FAILS_BEFORE_SLOWING = 3
MAX_DOUBLINGS = 16  # the slowing exponent is capped, so a long run of failures can never overflow
ENDS_POLL = frozenset({"timeout", "network"})  # no further request is sent in that poll
MAX_BODY_BYTES = 262_144
POLL_SESSIONS = (Session.PRE, Session.CORE, Session.POST)
STALE_CODES = frozenset({"old", "no_recent_bar"})  # the service answered; its newest data is only too old
NOT_SENT = frozenset({"backoff", "dropped", "skipped", "host_cap", "no_http", "refused"})
_SIX = Decimal("0.000001")


@dataclass
class Leg:
    """One request of a poll: `code` is "ok" or a short reason; `status` the HTTP status if any."""

    name: str
    code: str
    status: int | None = None
    sent: bool = False
    prices: dict[str, Price] = field(default_factory=dict)
    closes: dict[str, Decimal] = field(default_factory=dict)
    body: bytes | None = None  # kept only for a body the parser refused


@dataclass
class PollOutcome:
    """What one poll found. `state`: recorded, stale, failed or idle (no request sent)."""

    state: str
    reason: str | None
    day: date
    prev_day: date
    legs: dict[str, Leg]
    problems: dict[str, str]
    chosen: dict[str, Any] | None = None
    record: dict[str, Any] | None = None
    checked_at: datetime | None = None
    closes: dict[str, Decimal] = field(default_factory=dict)  # the previous closes this poll could use

    @property
    def sent(self) -> bool:
        return any(leg.sent for leg in self.legs.values())


def market_day(now: datetime) -> date:
    """The trading day whose prices are the newest at `now`: today from 04:00 ET on a trading day,
    else the trading day before."""
    today = et_date(now)
    start = pre_open(today) if is_trading_day(today) else None
    return today if start is not None and now >= start else previous_trading_day(today)


def pre_market(at: datetime | None) -> bool:
    """True in the pre-market session (04:00 to 09:30 ET on a trading day), where old prices are normal
    until IEX starts trading at about 08:00 ET."""
    return at is not None and session_at(at) == Session.PRE


def _fraction(last: Decimal, prev: Decimal) -> str:
    return format((last / prev - 1).quantize(_SIX, rounding=ROUND_HALF_UP), "f")


def candidate(feed: str, prices: Mapping[str, Price], closes: Mapping[str, Decimal], symbols: Iterable[str],
              now: datetime, day_start: datetime | None) -> tuple[dict[str, Any] | None, str]:
    """(the candidate, "") or (None, why): every symbol needs a price, a previous close, a trade time
    no later than `FUTURE_TOLERANCE_S` (60 s) ahead of `now` and no earlier than `day_start`."""
    out: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        p = prices.get(sym)
        if p is None:
            return None, "no_recent_bar" if feed == DELAYED else "missing_symbol"
        prev = closes.get(sym)
        if prev is None:
            return None, "no_prev_close"
        if (p.trade_at - now).total_seconds() > FUTURE_TOLERANCE_S:
            return None, "future_time"
        if day_start is not None and p.trade_at < day_start:
            return None, "old"
        out[sym] = {"last": p.text, "trade_at": p.trade_at, "prev_close": format(prev, "f"),
                    "change_pct": _fraction(p.last, prev)}
    if not out:
        return None, "missing_symbol"
    return {"feed": feed, "at": min(v["trade_at"] for v in out.values()), "symbols": out}, ""


def choose(candidates: Iterable[Mapping[str, Any] | None]) -> dict[str, Any] | None:
    """The newest `at`; a tie goes to the delayed (whole-market) prices."""
    found = [dict(c) for c in candidates if c]
    return max(found, key=lambda c: (c["at"], c["feed"] == DELAYED)) if found else None


def market_record(chosen: Mapping[str, Any], first_seen: datetime, day: date, prev_day: date) -> dict[str, Any]:
    """The `market` line for `chosen` (see the module doc)."""
    syms = {s: {"last": v["last"], "trade_at": iso_us(v["trade_at"]), "prev_close": v["prev_close"],
                "change_pct": v["change_pct"]} for s, v in chosen["symbols"].items()}
    return {"format": FORMAT, "kind": "market", "first_seen_at": iso_us(first_seen), "at": iso_us(chosen["at"]),
            "feed": chosen["feed"], "prev_close_feed": PREV_CLOSE_FEED, "prev_day": prev_day.isoformat(),
            "trading_day": day.isoformat(), "symbols": syms}


def miss_line(first_seen: datetime, reason: str, legs: Mapping[str, Leg]) -> dict[str, Any]:
    """The `miss` line of a poll that sent a request and recorded nothing: codes only."""
    return {"format": FORMAT, "kind": "miss", "first_seen_at": iso_us(first_seen), "reason": reason,
            "legs": {name: leg.code for name, leg in legs.items()}}


def _fetch_code(result: Any) -> str:
    if result.result == "http_error":
        return f"http_{result.status}" if result.status else "http_error"
    return str(result.result)


class MarketPoller:
    """One market feed (the job's, or the check command's with `write=False`)."""

    def __init__(self, cfg: Any, paths: Any, fetcher: Any, key: Any, clock: Clock, *, raw: Any = None,
                 log: logging.Logger | None = None, write: bool = True) -> None:
        self.cfg, self.paths, self.fetcher, self.key, self.clock = cfg, paths, fetcher, key, clock
        self.raw, self.write = raw, write
        self.log = log or logging.getLogger("scout.market")
        self.symbols = tuple(cfg.reads.market_symbols)
        self._day: date | None = None
        self._dropped: set[str] = set()
        self._closes: dict[str, Decimal] | None = None
        self._fails = 0
        self._state: tuple[str, str | None] | None = None
        self._leg_codes: dict[str, str] = {}
        self._broken = False

    def __repr__(self) -> str:  # never shows the key
        return f"MarketPoller(symbols={self.symbols!r}, fails={self._fails})"

    # ------------------------------------------------------------------ schedule

    def interval_at(self, now: datetime) -> float:
        base = float(self.cfg.reads.market_poll_s)
        if self._fails < FAILS_BEFORE_SLOWING:
            return base
        slowed = base * 2 ** min(self._fails - FAILS_BEFORE_SLOWING + 1, MAX_DOUBLINGS)
        return min(slowed, max(base, float(self.cfg.source_backoff_max_min) * 60.0))

    def tick(self, now: datetime) -> None:
        """The job: one poll in the PRE, CORE and POST sessions, and its line. Never raises."""
        try:
            if session_at(now) not in POLL_SESSIONS:
                return
            out = self.poll(now)
            if self.write:
                self._write(out)
            self._broken = False
        except Exception as exc:  # noqa: BLE001 - nothing raises into the scheduler
            if not self._broken:
                self.log.warning("market feed: a poll failed unexpectedly (%s); trying again next poll",
                                 type(exc).__name__)
            self._broken = True

    # ------------------------------------------------------------------ one poll

    def _new_day(self, day: date) -> None:
        if self._day != day:
            self._day, self._closes = day, None
            self._dropped.clear()

    def _get(self, name: str, url: str, parse: Callable[[bytes | None], Any]) -> Leg:
        if name in self._dropped:
            return Leg(name, "dropped")
        result = self.fetcher.fetch(SOURCE, url, conditional=False, max_bytes=MAX_BODY_BYTES,
                                    headers=self.key.headers(), user_agent=f"Scout/{__version__}",
                                    timeout_s=self.cfg.reads.market_timeout_s, quiet=True)
        code = _fetch_code(result)
        leg = Leg(name, code, status=result.status, sent=code not in NOT_SENT)
        if code != "ok":
            return leg
        try:
            parsed = parse(result.body)
        except SchemaError:
            leg.code, leg.body = "schema", result.body
            return leg
        if name == CLOSES:
            leg.closes = parsed
        else:
            leg.prices = parsed
        return leg

    def _legs(self, now: datetime, day: date, prev_day: date) -> dict[str, Leg]:
        syms = self.symbols
        asks: list[tuple[str, str, Callable[[bytes | None], Any]]] = [
            (LIVE, live_url(syms), lambda b: parse_snapshots(b, syms)),
            (DELAYED, delayed_url(syms, now), lambda b: parse_minute_bars(b, syms))]
        if self._closes is None:
            asks.append((CLOSES, closes_url(syms, prev_day, day), lambda b: parse_closes(b, syms, prev_day)))
        legs: dict[str, Leg] = {}
        for name, url, parse in asks:  # a timeout or network failure on any request ends the poll
            ended = any(leg.code in ENDS_POLL for leg in legs.values())
            legs[name] = Leg(name, "skipped") if ended else self._get(name, url, parse)
        if CLOSES in legs:
            got = legs[CLOSES]
            if got.code == "ok" and all(s in got.closes for s in syms):
                self._closes = dict(got.closes)
            elif got.code == "ok":
                got.code = "no_prev_close"
        answered = {leg.name for leg in legs.values() if leg.status == 200}
        for leg in legs.values():  # 403/422 while another request of this poll was answered: not asked again today
            if leg.status in (403, 422) and answered - {leg.name}:
                self._dropped.add(leg.name)
                self.log.warning("market feed: Alpaca refused the %s request (HTTP %s) while it answers the others; "
                                 "not asked again until tomorrow", _LEG_WORDS[leg.name], leg.status)
        return legs

    def poll(self, now: datetime) -> PollOutcome:
        """One poll in any session (the job's and the check command's). It writes no line itself."""
        day = market_day(now)
        prev_day = previous_trading_day(day)
        self._new_day(et_date(now))
        legs = self._legs(now, day, prev_day)
        done = self.clock.now()
        closes = self._closes or {}
        cands: dict[str, dict[str, Any]] = {}
        problems: dict[str, str] = {}
        for name in PRICE_LEGS:
            leg = legs[name]
            if leg.code != "ok":
                problems[name] = leg.code
                continue
            cand, why = candidate(name, leg.prices, closes, self.symbols, done, pre_open(day))
            if cand is None:
                problems[name] = why
            else:
                cands[name] = cand
        chosen = choose(cands.values())
        some_closes = closes or (legs[CLOSES].closes if CLOSES in legs else {})  # partial ones too, for the check
        out = PollOutcome("idle", None, day, prev_day, legs, problems, chosen=chosen, checked_at=done,
                          closes=dict(some_closes))
        if chosen is not None:
            fresh = (done - chosen["at"]).total_seconds() <= self.cfg.reads.market_max_age_s
            # the only candidate is too old while another price request failed outright: a failed poll
            outright = [legs[n].code for n in PRICE_LEGS if n not in cands and legs[n].sent and legs[n].code != "ok"]
            if fresh:
                out.state, out.reason = "recorded", None
                out.record = market_record(chosen, done, day, prev_day)
            else:
                out.state, out.reason = ("failed", outright[0]) if outright else ("stale", "stale")
        elif out.sent:
            out.state, out.reason = self._failure(legs, problems)
        else:
            out.reason = next((leg.code for leg in legs.values() if leg.code != "ok"), None)
        self._after(out)
        return out

    def _failure(self, legs: Mapping[str, Leg], problems: Mapping[str, str]) -> tuple[str, str]:
        statuses = [leg.status for leg in legs.values() if leg.sent]
        if 200 not in statuses and any(s in (401, 403) for s in statuses):
            return "failed", "refused_key"
        found = [problems[n] for n in PRICE_LEGS if n in problems and problems[n] not in NOT_SENT]
        hard = [p for p in found if p not in STALE_CODES]
        if hard:
            return "failed", hard[0]
        if found:
            return "stale", "stale"
        if CLOSES in legs and legs[CLOSES].code not in ("ok", *NOT_SENT):
            return "failed", "no_prev_close"
        return "failed", next((leg.code for leg in legs.values() if leg.code != "ok"), "unknown")

    # ------------------------------------------------------------------ after a poll

    def _after(self, out: PollOutcome) -> None:
        if out.state == "failed":
            self._fails += 1
        elif out.state in ("recorded", "stale"):
            self._fails = 0
        for name, leg in out.legs.items():
            hard = leg.code not in ("ok", *NOT_SENT, *STALE_CODES)
            if hard and self._leg_codes.get(name) != leg.code and leg.code == "schema" and leg.body is not None:
                self._save_refused(name, leg.body)
            if leg.code not in NOT_SENT:
                self._leg_codes[name] = leg.code
        if out.state != "idle":
            self._note(out)

    def _save_refused(self, name: str, body: bytes) -> None:
        if self.raw is None or not self.write:
            return
        try:
            self.raw.save(SOURCE, f"{name}-refused.json", body, self.clock.now())
        except OSError as exc:
            self.log.warning("market feed: could not keep the refused answer (%s)", type(exc).__name__)

    def _note(self, out: PollOutcome) -> None:
        """Log once per change of (state, reason)."""
        state = ("ok" if out.state == "recorded" else out.state, out.reason)
        if state == self._state:
            return
        before, self._state = self._state, state
        syms = ", ".join(self.symbols)
        if out.state == "recorded":
            feed = out.chosen["feed"] if out.chosen else "?"
            self.log.info("market feed: %s %s (%s)", "recording again" if before else "recording", syms,
                          _FEED_WORDS.get(feed, feed))
        elif out.state == "stale":
            early = " (normal before about 08:00 ET)" if pre_market(out.checked_at) else ""
            self.log.info("market feed: Alpaca's newest prices are older than %d minutes, so they are not shown to "
                          "readers%s; polls go on", self.cfg.reads.market_max_age_s // 60, early)
        elif out.reason == "refused_key":
            self.log.warning("Alpaca refused the key: check the two lines in Scout's .env")
        else:
            detail = "; ".join(f"{n}: {leg.code}" for n, leg in out.legs.items())
            self.log.warning("market feed: no usable answer (%s); reads are shown no market from Scout until it "
                             "recovers", detail)

    def _write(self, out: PollOutcome) -> None:
        now = self.clock.now()
        if out.state == "recorded" and out.record is not None:
            line = dict(out.record, first_seen_at=iso_us(now))
        elif out.sent and out.state in ("stale", "failed"):
            line = miss_line(now, str(out.reason), out.legs)
        else:
            return
        try:
            append_jsonl(self.paths, market_file(self.paths, et_date(now)), line)
        except OSError as exc:
            self.log.warning("market feed: line not written (%s)", type(exc).__name__)


_LEG_WORDS = {LIVE: "live (IEX)", DELAYED: "whole-market (20 minutes late)", CLOSES: "previous-close"}
_FEED_WORDS = {LIVE: "live prices from the IEX exchange",
               DELAYED: "whole-market prices at least 20 minutes old"}


# --------------------------------------------------------------------------- the check command


def cli_reads_market(ctx: Any, args: Any) -> int:
    """`reads market` (Check Market.bat): one poll now; says whether Alpaca accepted the key and
    answered, the newest prices with their age, and what a reader would see now. It writes no record
    or state (no market or miss line, no raw file, no fetch state), only its log lines. 0 the key
    works, 1 no request answered readably, 2 not set up (`scout.market_check`)."""
    from scout.market_check import run_check  # noqa: PLC0415

    return run_check(ctx, args)


def _age_words(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 120:
        return f"{s} second{'s' if s != 1 else ''}"
    if s < 7200:
        return f"{s // 60} minutes"
    minutes = s % 3600 // 60
    return f"{s // 3600} hours {minutes} minute{'s' if minutes != 1 else ''}"


def stamp_age(at: Any, now: datetime) -> str:
    """How old a stored time is, in words ("3 seconds", "17 minutes", "14 hours 2 minutes")."""
    when = parse_iso(at) if isinstance(at, str) else at
    return "unknown" if when is None else _age_words((now - when).total_seconds())
