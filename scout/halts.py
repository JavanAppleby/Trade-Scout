"""Nasdaq Trader halt RSS (SCOUT_SPEC 4.1 #6, 6.3, 7.5; DESIGN 2.3). Record only: no bot gates on
halts in v1, and Guardian's broker refusal stays authoritative.

Parsing (SCOUT_SPEC 6.3): strip the BOM; elements in the `ndaq` namespace; Market is "NASDAQ" or
one letter; HaltTime is ET with optional fractional seconds, converted to UTC once here via
`scout_reader._et`; `pubDate` is ignored; no item order is assumed; an empty `ResumptionTradeTime`
means still halted; a multi-day halt appears only under its first HaltDate. Rows are stored as
received (the feed must not be edited or misrepresented): each fact keeps the row's fields verbatim
in `extra.row`. Drift (no channel, a missing field, an unreadable date or time, an unknown Market
form) raises `SchemaError`; the poll then records `schema` and no facts.

Polling: at least 60 s between requests (`HARD_MIN_SPACING_S`, in code, whatever the config says),
04:00-20:00 ET, plus one `&haltdate=MMDDYYYY` read after 20:00 ET (the job decides when).
"""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from scout_reader._et import et_to_utc

from scout.clock import Clock, et_date, iso
from scout.config import ScoutConfig
from scout.edgar_parse import refuse_dtd
from scout.macro import SchemaError, fact_id
from scout.models import Fact, FetchResult, HealthRecord, SourceState

log = logging.getLogger("scout.halts")

SOURCE = "nasdaq_halts"
HARD_MIN_SPACING_S = 60.0  # Nasdaq: "do not query more than once a minute"
FEED_LIVE = "tradehalts"
FEED_DATED = "tradehalts_dated"
REQUIRED = ("HaltDate", "HaltTime", "IssueSymbol", "Market", "ReasonCode", "ResumptionTradeTime")
OPTIONAL = ("IssueName", "PauseThresholdPrice", "ResumptionDate", "ResumptionQuoteTime")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$")
_MARKET = re.compile(r"^(NASDAQ|[A-Z])$")

__all__ = ["FEED_DATED", "FEED_LIVE", "HARD_MIN_SPACING_S", "HaltPoller", "HaltRow", "SOURCE", "dated_url",
           "halt_key", "parse_halts"]


@dataclass(frozen=True)
class HaltRow:
    symbol: str
    halt_at: datetime  # aware UTC
    halt_date: str  # as received, "MM/DD/YYYY"
    halt_time: str  # as received
    market: str
    reason: str
    resumed_at: datetime | None  # aware UTC; None while halted
    issue_name: str | None
    row: dict[str, str]  # every ndaq field, verbatim

    @property
    def key(self) -> str:
        return halt_key(self.symbol, self.halt_date, self.halt_time)


def halt_key(symbol: str, halt_date: str, halt_time: str) -> str:
    return f"{symbol}|{halt_date}|{halt_time}"


def _day(text: str, where: str) -> date:
    m = _DATE.match(text.strip())
    if not m:
        raise SchemaError(f"halts {where}: unreadable date {text[:20]!r}")
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError as exc:
        raise SchemaError(f"halts {where}: impossible date {text[:20]!r}") from exc


def _et(day: date, text: str, where: str) -> datetime:
    m = _TIME.match(text.strip())
    if not m:
        raise SchemaError(f"halts {where}: unreadable time {text[:20]!r}")
    hh, mm, ss, frac = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        naive = datetime(day.year, day.month, day.day, int(hh), int(mm), int(ss), micro)
    except ValueError as exc:
        raise SchemaError(f"halts {where}: impossible time {text[:20]!r}") from exc
    return et_to_utc(naive)


def _local(tag: str) -> tuple[str | None, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return None, tag


def parse_halts(body: bytes) -> list[HaltRow]:
    """Every halt row of the feed (any order). Raises SchemaError on drift."""
    if not isinstance(body, (bytes, bytearray)):
        raise SchemaError("halts: body is not bytes")
    data = bytes(body).lstrip()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:].lstrip()
    refuse_dtd(data, "halts", SchemaError)  # any encoding (a UTF-16 body passed the byte search; third review)
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError, LookupError) as exc:
        raise SchemaError("halts: not well-formed XML") from exc
    if _local(root.tag)[1] != "rss":
        raise SchemaError("halts: root is not <rss>")
    channel = root.find("channel")
    if channel is None:
        raise SchemaError("halts: no <channel>")
    rows: list[HaltRow] = []
    for i, item in enumerate(channel.findall("item")):
        fields: dict[str, str] = {}
        for child in item:
            ns, name = _local(child.tag)
            if ns:  # the ndaq namespace; pubDate, title and the rest are not read
                fields[name] = (child.text or "").strip()
        where = f"item {i}"
        missing = [k for k in REQUIRED if k not in fields]
        if missing:
            raise SchemaError(f"halts {where}: missing {', '.join(missing)}")
        symbol = fields["IssueSymbol"].strip()
        if not symbol:
            raise SchemaError(f"halts {where}: empty IssueSymbol")
        market = fields["Market"].strip()
        if not _MARKET.match(market):
            raise SchemaError(f"halts {where}: unknown Market form {market[:12]!r}")
        hday = _day(fields["HaltDate"], where)
        halt_at = _et(hday, fields["HaltTime"], where)
        resumed_at = None
        if fields["ResumptionTradeTime"].strip():
            rdate = fields.get("ResumptionDate", "").strip()
            rday = _day(rdate, where) if rdate else hday
            resumed_at = _et(rday, fields["ResumptionTradeTime"], where)
        name = fields.get("IssueName")
        rows.append(HaltRow(symbol=symbol, halt_at=halt_at, halt_date=fields["HaltDate"].strip(),
                            halt_time=fields["HaltTime"].strip(), market=market, reason=fields["ReasonCode"].strip(),
                            resumed_at=resumed_at, issue_name=name or None, row=dict(fields)))
    return rows


def dated_url(url: str, day: date) -> str:
    """The feed URL with `&haltdate=MMDDYYYY` for ET `day`."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}haltdate={day.month:02d}{day.day:02d}{day.year:04d}"


class HaltPoller:
    """Polls the halt feed and writes `halt` facts: one when a halt is first seen (with
    `resumed_at` when already resumed) and one more when a resumption appears later."""

    def __init__(self, cfg: ScoutConfig, fetcher: Any, raw: Any, facts: Any, health: Any, board: Any,
                 clock: Clock) -> None:
        self.cfg, self.fetcher, self.raw, self.facts = cfg, fetcher, raw, facts
        self.health, self.board, self.clock = health, board, clock
        self.src = cfg.source(SOURCE)
        self.spacing_s = max(HARD_MIN_SPACING_S, float(self.src.min_spacing_s))
        self.last_request: datetime | None = None
        self.last_ok: str | None = None
        self.last_sha: str | None = None
        self.dated_done: date | None = None
        self.last_result: str | None = None
        self.seen_halts: set[str] = set()
        self.seen_resumed: set[str] = set()
        self.refetch_full = False  # a fact write failed: the next read is unconditional, to see it again

    def _last_sent(self) -> datetime | None:
        """The later of this poller's last request and the fetcher's last send to the host. The
        fetcher's time is when the request really went out (after earlier jobs in the tick) and is
        kept across restarts, so a restart never polls within 60 s and a slow tick never makes the
        fetcher sleep inside the loop to keep the spacing."""
        times = [self.last_request]
        sent_at = getattr(self.fetcher, "last_request_at", None)
        if callable(sent_at):
            try:
                host = (urllib.parse.urlsplit(self.src.url).hostname or "").lower()
                times.append(sent_at(host))
            except Exception:  # noqa: BLE001 - a fake fetcher: our own time stands
                pass
        found = [t for t in times if isinstance(t, datetime)]
        return max(found) if found else None

    def ready(self, now: datetime) -> bool:
        """The 60 s hard minimum (in code) since the last request. A last request in the future (the
        wall clock stepped back) counts as long past, so halts do not stop until the clock catches up."""
        last = self._last_sent()
        if last is None:
            return True
        elapsed = (now - last).total_seconds()
        return elapsed < 0 or elapsed >= self.spacing_s - 1e-6

    def poll(self, now: datetime) -> int:
        """One read of the live feed; the number of new facts."""
        return self._read(now, self.src.url, FEED_LIVE)

    def dated_read(self, now: datetime) -> int:
        """The once-a-day `&haltdate=` read for today's ET date (after 20:00 ET). Marked done only
        after a good result, so a failed read is tried again on a later tick (60 s spacing kept)."""
        day = et_date(now)
        if self.dated_done == day:
            return 0
        if not self.ready(now):
            return 0
        self.last_result = None
        new = self._read(now, dated_url(self.src.url, day), FEED_DATED)
        if self.last_result in ("ok", "not_modified"):
            self.dated_done = day
        return new

    def _read(self, now: datetime, url: str, feed: str) -> int:
        if not self.ready(now):
            return 0
        previous, self.last_request = self.last_request, now
        got: FetchResult = self.fetcher.fetch(SOURCE, url, conditional=feed == FEED_LIVE and not self.refetch_full)
        gap = previous is not None and (now - previous).total_seconds() > max(
            float(self.cfg.source_max_gap_s), 2 * self.spacing_s)
        rows: list[HaltRow] = []
        result, error = got.result, got.error
        sha = None
        if got.ok and got.body is not None:
            try:
                rows = parse_halts(got.body)
            except SchemaError as exc:
                result, error = "schema", str(exc)[:200]
            else:
                sha = self.raw.save(SOURCE, f"{feed}.xml", got.body, now) if self._changed(got.body) else self.last_sha
        try:
            new = self._facts(now, rows, sha, url, feed) if result == "ok" else 0
        except Exception:
            self.refetch_full = True  # a 304 would hide the rows whose write failed
            raise
        if result == "ok":
            self.refetch_full = False
        self.last_result = result
        self._health(got, feed, result, len(rows), new, gap, error)
        good = result in ("ok", "not_modified")
        if good:
            self.last_ok = iso(now)
        self.board.update(SOURCE, SourceState(enabled=self.src.enabled, status="ok" if good else "unavailable",
                                              last_ok=self.last_ok, last_error=None if good else (error or result),
                                              gap_open=not good))
        return new

    def _changed(self, body: bytes) -> bool:
        digest = hashlib.sha256(body).hexdigest()
        if digest == self.last_sha:
            return False
        self.last_sha = digest
        return True

    def _fact(self, now: datetime, row: HaltRow, guid: str, sha: str | None, url: str, feed: str,
              resumed: datetime | None) -> Fact:
        return Fact(fact_id=fact_id(SOURCE, guid, row.symbol), kind="halt", ticker=row.symbol.upper(), cik=None,
                    cls=None, source=SOURCE, feed=feed, guid=guid, url=url, title=row.issue_name,
                    published_at=iso(row.halt_at), first_seen_at=iso(now), raw_sha256=sha,
                    halt_code=row.reason or None, halt_time=iso(row.halt_at),
                    resumed_at=iso(resumed) if resumed else None, halt_market=row.market,
                    extra={"halt_key": row.key, "row": dict(row.row)})

    def _facts(self, now: datetime, rows: list[HaltRow], sha: str | None, url: str, feed: str) -> int:
        """A key is marked seen only after `facts.add` returns: a failed write raises, and the next
        poll tries the row again (the FactSink contract the wire and EDGAR pollers keep)."""
        new = 0
        for row in rows:
            key = row.key
            rkey = f"{key}|{iso(row.resumed_at)}" if row.resumed_at is not None else None
            if key not in self.seen_halts:
                stored = self.facts.add(self._fact(now, row, key, sha, url, feed, row.resumed_at))
                self.seen_halts.add(key)
                if stored:
                    new += 1
                    if rkey is not None:
                        self.seen_resumed.add(rkey)  # the halt fact already carries it
                # else: stored before (e.g. before a restart), maybe without this resumption
            if rkey is None or rkey in self.seen_resumed:
                continue
            fact = self._fact(now, row, f"{key}|resumed|{iso(row.resumed_at)}", sha, url, feed, row.resumed_at)
            if self.facts.add(fact):
                new += 1
            self.seen_resumed.add(rkey)
        return new

    def _health(self, got: FetchResult, feed: str, result: str, items: int, new: int, gap: bool,
                error: str | None) -> None:
        try:
            self.health.record(HealthRecord(source=SOURCE, feed=feed, kind=self.src.kind, enabled=self.src.enabled,
                                            gate6=False, start=got.started_at, end=got.ended_at, result=result,
                                            http_status=got.status, items=items, new_items=new, overlap=None,
                                            spacing_s=self.spacing_s, gap_before=bool(gap), error=error))
        except Exception as exc:  # noqa: BLE001 - a health record never stops a poll
            log.warning("halts health record failed: %s", type(exc).__name__)

