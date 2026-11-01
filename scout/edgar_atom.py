"""EDGAR "getcurrent" Atom poller (SCOUT_SPEC 4.1 #1, 6.1-6.4; DESIGN 2.2, 7, D6).

One request per form in `edgar_atom_forms` (`type=<form>&count=100`), each a separate feed for the
overlap rule: market-wide Form 4 traffic would otherwise push filings off a single page within a
poll. Each new entry becomes a fact (`kind = "filing"`): ticker and exchange from the CIK map, class
from the SS 6.4 filing table, 8-K items from the summary, `published_at` = EDGAR's acceptance time.

The poll is skipped while the CIK-to-ticker map is not loaded: filing facts without tickers would
let gate 6 answer `false` for a company whose filing Scout saw. The source then stays `stale` and
its coverage gap stays open, so gate 6 answers `unknown`.

Whether `type=424B` returns every 424B* form is UNVERIFIED (D6): the poller keeps every entry the
page returns, whatever its form, and the fixture marks the shape as synthetic until a live capture.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime

from scout.clock import Clock, iso
from scout.config import ScoutConfig
from scout.edgar_parse import AtomEntry, SchemaError, filing_class, parse_getcurrent_atom
from scout.facts import make_fact_id
from scout.health import CoverageTracker, board_state, status_for
from scout.interfaces import FactSink, Fetcher, HealthSink, RawSink, StatusBoard, TickerMap
from scout.models import Fact

__all__ = ["SOURCE", "EdgarAtomPoller", "form_url"]

SOURCE = "edgar_atom"


def form_url(template: str, form: str) -> str:
    """The source URL with `{form}` filled in, URL-quoted ("NT 10-Q" -> "NT%2010-Q")."""
    return template.replace("{form}", urllib.parse.quote(form, safe=""))


class EdgarAtomPoller:
    def __init__(self, cfg: ScoutConfig, fetcher: Fetcher, raw: RawSink, facts: FactSink, health: HealthSink,
                 board: StatusBoard, ticker_map: TickerMap, clock: Clock, *, tracker: CoverageTracker | None = None,
                 log: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.src = cfg.source(SOURCE)
        self.fetcher, self.raw, self.facts, self.health, self.board = fetcher, raw, facts, health, board
        self.ticker_map = ticker_map
        self.clock = clock
        self.tracker = tracker or CoverageTracker(cfg)
        self.log = log or logging.getLogger("scout.edgar_atom")
        self.last_ok: str | None = None
        self.last_error: str | None = None

    def _publish(self, status: str, now: datetime) -> None:
        self.board.update(SOURCE, board_state(self.src.enabled, status, self.last_ok, self.last_error,
                                              self.tracker.gap_open(SOURCE, now)))

    def _map_ready(self) -> bool:
        ready = getattr(self.ticker_map, "ready", None)
        return bool(ready()) if callable(ready) else True

    def poll(self, now: datetime) -> int:
        """Poll every configured form once; returns the number of new facts."""
        if not self.src.enabled:
            self._publish("disabled", now)
            return 0
        if not self._map_ready():
            self.last_error = "the CIK-to-ticker map is not loaded yet"
            self.log.info("edgar_atom: skipped (%s)", self.last_error)
            self._publish("stale", now)
            return 0
        results: list[str] = []
        added = 0
        for form in self.cfg.edgar_atom_forms:
            try:
                got, result = self._poll_form(form, now)
            except Exception as exc:  # noqa: BLE001 - one form's failure never stops the later forms
                self.last_error = f"{form}: {type(exc).__name__}"
                self.log.warning("edgar_atom %s: poll failed (%s)", form, type(exc).__name__)
                got, result = 0, "network"
            added += got
            results.append(result)
        if all(r in ("ok", "not_modified") for r in results):
            self.last_ok = iso(self.clock.now())
        self._publish(status_for(results), now)
        return added

    def _poll_form(self, form: str, now: datetime) -> tuple[int, str]:
        fetch = self.fetcher.fetch(SOURCE, form_url(self.src.url, form))
        if fetch.result == "not_modified":
            self.health.record(self.tracker.observe(self.src, form, fetch, now=now))
            return 0, fetch.result
        if fetch.result != "ok":
            self.last_error = f"{form}: {fetch.error or fetch.result}"
            self.health.record(self.tracker.observe(self.src, form, fetch, now=now))
            return 0, fetch.result
        try:
            page = parse_getcurrent_atom(fetch.body or b"")
        except SchemaError as exc:
            self.last_error = f"{form}: schema: {exc}"
            self.log.warning("edgar_atom %s: page shape changed (%s); source unavailable", form, exc)
            self.health.record(self.tracker.observe(self.src, form, fetch, now=now, result="schema", error=str(exc)))
            return 0, "schema"
        new = set(self.tracker.new_guids(SOURCE, form, (e.guid for e in page.entries)))
        n_new = len(new)
        added = 0
        for entry in page.entries:
            if entry.guid in new:
                added += self._store(entry, form, now, fetch.ended_at)
                new.discard(entry.guid)
        self.health.record(self.tracker.observe(self.src, form, fetch, now=now, guids=[e.guid for e in page.entries],
                                                new_items=n_new, page_size=page.total))
        return added, "ok"

    def _listings(self, cik: int) -> list[tuple[str | None, str | None]]:
        """Every (ticker, exchange) under the CIK (one fact each), or one (None, None)."""
        many = getattr(self.ticker_map, "tickers_for_cik", None)
        found = list(many(cik)) if callable(many) else []
        if not found:
            one = self.ticker_map.ticker_for_cik(cik)
            found = [one] if one else []
        return [(t, e) for t, e in found] or [(None, None)]

    def _store(self, entry: AtomEntry, feed: str, now: datetime, seen_at: str) -> int:
        digest = self.raw.save(SOURCE, f"{entry.accession}-{entry.cik}.xml", entry.raw, now)
        cls = filing_class(entry.form, entry.items, f"{entry.title} {entry.summary}")
        added = 0
        for ticker, exchange in self._listings(entry.cik):
            fact = Fact(fact_id=make_fact_id(SOURCE, entry.guid, ticker), kind="filing", ticker=ticker,
                        cik=entry.cik, cls=cls, source=SOURCE, feed=feed, guid=entry.guid, url=entry.link,
                        title=entry.title, published_at=entry.accepted_at, first_seen_at=seen_at,
                        raw_sha256=digest, exchange=exchange, form=entry.form, items=entry.items,
                        accession=entry.accession, extra={"company": entry.company, "role": entry.role,
                                                          "filed_on": entry.filed_on})
            added += int(bool(self.facts.add(fact)))
        return added
