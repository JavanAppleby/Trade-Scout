"""RSS wires: PR Newswire and Stock Titan (SCOUT_SPEC 4.1 #4, 6.1-6.4; DESIGN 2.2, 7).

One poll: fetch the feed -> parse (a changed shape is a SchemaError: health "schema", status
`unavailable`, no facts) -> the items not seen before -> raw store (title, link, guid and pubDate
only while `terms_ok` is false) -> tickers from the exchange tags in the title, the description and
any "EXCHANGE:TICKER" category, then the "| TICKER Stock News" end of a title (no exchange) -> one
fact per ticker (one with no ticker when none is named) -> the health record (overlap, gap) and the
status board.

The description is read in memory for its exchange tags only; it is never stored. A source that
answers 403 `refused_after_403s` times in a row is marked `refused` and not polled again that ET
day.
"""

from __future__ import annotations

import email.utils
import hashlib
import logging
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from scout.classify import wire_class
from scout.clock import Clock, et_date, iso
from scout.config import ScoutConfig
from scout.edgar_parse import SchemaError, safe_xml_root
from scout.facts import make_fact_id
from scout.health import CoverageTracker, board_state, status_for
from scout.interfaces import FactSink, Fetcher, HealthSink, RawSink, StatusBoard
from scout.models import Fact
from scout.raw import keeps_full_body, reduced_item
from scout.tickers import tickers_in, title_suffix_ticker

__all__ = ["RssItem", "WirePoller", "parse_rss"]

FEED = "rss"


@dataclass(frozen=True)
class RssItem:
    guid: str
    title: str
    link: str | None
    published_at: str  # UTC "...Z"
    raw_categories: tuple[str, ...] = ()
    description: str = ""  # in memory only (exchange tags); never stored
    pub_date: str = ""  # the pubDate text as received
    raw: bytes = b""  # the item as received (re-serialised)


def _published(text: str) -> datetime:
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError) as exc:
        raise SchemaError("rss: an item has an unreadable pubDate") from exc
    if dt is None:
        raise SchemaError("rss: an item has an unreadable pubDate")
    if dt.tzinfo is None:  # "-0000" means UTC with no zone information (RFC 5322)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_rss(body: bytes) -> list[RssItem]:
    """An RSS 2.0 page -> its items, in feed order. BOM stripped. Every item must have a title, a
    guid (or link) and a readable pubDate; otherwise the whole page is a SchemaError."""
    root = safe_xml_root(body)
    if root.tag != "rss":
        raise SchemaError(f"rss: root is {root.tag!r}, not rss")
    channel = root.find("channel")
    if channel is None:
        raise SchemaError("rss: no channel")
    out: list[RssItem] = []
    for el in channel.findall("item"):
        title = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip() or None
        guid = (el.findtext("guid") or "").strip() or link
        pub = (el.findtext("pubDate") or "").strip()
        if not title or not guid or not pub:
            raise SchemaError("rss: an item lacks a title, guid or pubDate")
        cats = tuple((c.text or "").strip() for c in el.findall("category") if (c.text or "").strip())
        out.append(RssItem(guid=guid, title=title, link=link, published_at=iso(_published(pub)),
                           raw_categories=cats, description=(el.findtext("description") or ""), pub_date=pub,
                           raw=ET.tostring(el, encoding="utf-8")))
    return out


def item_tickers(item: RssItem, prefixes: Sequence[str],
                 suffixes: Sequence[str] = ()) -> list[tuple[str | None, str]]:
    """(exchange, ticker) pairs from the title, the description and "EXCH:TICKER" categories, then
    (None, ticker) from a "| TICKER Stock News" title end when that ticker is not already named."""
    text = " ".join([item.title, item.description, *(f"({c})" for c in item.raw_categories)])
    pairs: list[tuple[str | None, str]] = list(tickers_in(text, prefixes))
    end = title_suffix_ticker(item.title, suffixes)
    if end is not None and all(t != end for _, t in pairs):
        pairs.append((None, end))
    return pairs


class WirePoller:
    """Polls one RSS wire source (`prnewswire`, `stocktitan`)."""

    def __init__(self, cfg: ScoutConfig, source_name: str, fetcher: Fetcher, raw: RawSink, facts: FactSink,
                 health: HealthSink, board: StatusBoard, clock: Clock, *, tracker: CoverageTracker | None = None,
                 log: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.src = cfg.source(source_name)
        self.fetcher, self.raw, self.facts, self.health, self.board = fetcher, raw, facts, health, board
        self.clock = clock
        self.tracker = tracker or CoverageTracker(cfg)
        self.log = log or logging.getLogger(f"scout.wire.{source_name}")
        self.forbidden_in_a_row = 0
        self.refused_on: date | None = None
        self.last_ok: str | None = None
        self.last_error: str | None = None

    @property
    def name(self) -> str:
        return self.src.name

    def _publish(self, status: str, now: datetime) -> None:
        self.board.update(self.name, board_state(self.src.enabled, status, self.last_ok, self.last_error,
                                                 self.tracker.gap_open(self.name, now)))

    def poll(self, now: datetime) -> int:
        """One poll; returns the number of new facts."""
        if not self.src.enabled:
            self._publish("disabled", now)
            return 0
        if self.refused_on is not None:
            if self.refused_on == et_date(now):
                self._publish("refused", now)
                return 0
            self.refused_on, self.forbidden_in_a_row = None, 0
        fetch = self.fetcher.fetch(self.name, self.src.url)
        if fetch.status == 403:
            self.forbidden_in_a_row += 1
        elif fetch.result in ("ok", "not_modified"):
            self.forbidden_in_a_row = 0
        if fetch.result not in ("ok", "not_modified"):
            result = fetch.result
            if self.forbidden_in_a_row >= self.cfg.refused_after_403s:
                self.refused_on = et_date(now)
                result = "refused"
                self.log.warning("%s refused %d requests in a row; not polled again today", self.name,
                                 self.forbidden_in_a_row)
            self.last_error = fetch.error or fetch.result
            self.health.record(self.tracker.observe(self.src, FEED, fetch, now=now, result=result))
            self._publish(status_for([result]), now)
            return 0
        if fetch.result == "not_modified":
            self.last_ok = fetch.ended_at
            self.health.record(self.tracker.observe(self.src, FEED, fetch, now=now))
            self._publish("ok", now)
            return 0
        try:
            items = parse_rss(fetch.body or b"")
        except SchemaError as exc:
            self.last_error = f"schema: {exc}"
            self.log.warning("%s: feed shape changed (%s); source unavailable", self.name, exc)
            self.health.record(self.tracker.observe(self.src, FEED, fetch, now=now, result="schema",
                                                    error=str(exc)))
            self._publish("unavailable", now)
            return 0
        new_list = self.tracker.new_guids(self.name, FEED, (i.guid for i in items))
        new_guids = set(new_list)
        added = 0
        seen_now = fetch.ended_at  # when Scout had the item (after any spacing wait), never earlier
        for item in items:
            if item.guid not in new_guids:
                continue
            added += self._store(item, seen_now, now)
            new_guids.discard(item.guid)  # a repeated guid on one page is stored once
        self.last_ok = fetch.ended_at
        self.health.record(self.tracker.observe(self.src, FEED, fetch, now=now, guids=[i.guid for i in items],
                                                new_items=len(new_list)))
        self._publish("ok", now)
        return added

    def _store(self, item: RssItem, seen_now: str, now: datetime) -> int:
        body = item.raw if keeps_full_body(self.cfg, self.name) else \
            reduced_item(item.title, item.link, item.guid, item.pub_date)
        digest = self.raw.save(self.name, f"{hashlib.sha256(item.guid.encode()).hexdigest()[:16]}.xml", body, now)
        cls = wire_class(item.title, self.cfg.class_keywords)
        pairs: list[tuple[str | None, str | None]] = list(item_tickers(item, self.cfg.ticker_exchange_prefixes,
                                                                       self.cfg.wire_title_ticker_suffixes))
        if not pairs:
            pairs = [(None, None)]
        added = 0
        for exchange, ticker in pairs:
            fact = Fact(fact_id=make_fact_id(self.name, item.guid, ticker), kind="wire", ticker=ticker, cik=None,
                        cls=cls, source=self.name, feed=FEED, guid=item.guid, url=item.link, title=item.title,
                        published_at=item.published_at, first_seen_at=seen_now, raw_sha256=digest,
                        exchange=exchange)
            added += bool(self.facts.add(fact))
        return added

