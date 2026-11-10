"""EDGAR "getcurrent" Atom poller (SCOUT_SPEC 4.1 #1, 6.1-6.4; DESIGN 2.2, 7, D6).

One request per form in `edgar_atom_forms` (`type=<form>&count=100`), each a separate feed for the
overlap rule: market-wide Form 4 traffic would otherwise push filings off a single page within a
poll. Each new entry becomes a fact (`kind = "filing"`): ticker and exchange from the CIK map, class
from the SS 6.4 filing table, 8-K items from the summary, `published_at` = EDGAR's acceptance time.

The poll is skipped while the CIK-to-ticker map is not loaded: filing facts without tickers would
let gate 6 answer `false` for a company whose filing Scout saw. The source then stays `stale` and
its coverage gap stays open, so gate 6 answers `unknown`.

Between two form requests the poller calls `between()` (owner, 2026-11-04; 0.2.2): `scout.jobs_wire`
passes one that runs the wire jobs that are due, in this same thread, so a slow SEC (10 s replies,
20 s timeouts: one cycle of 12 forms took ~140 s) no longer delays the wire polls past
`source_max_gap_s`. A wire then waits at most one EDGAR request. An exception in `between()` is
logged and never stops the EDGAR cycle.

Whether `type=424B` returns every 424B* form is UNVERIFIED (D6): the poller keeps every entry the
page returns, whatever its form, and the fixture marks the shape as synthetic until a live capture.

With the polling pace (`pace`, `scout.pace`; owner, 2026-11-10) each form has its own spacing: the
job runs at the fastest form's spacing (`interval_at`) and a cycle polls only the forms whose own
spacing has passed since their last poll, so a quiet form (F-3, NT 10-K) is read every few minutes
while 8-K stays at 60 s. The source's status and `last_ok` come from every form's latest result, so
a form that failed and is not due again keeps the source from reading "ok". Without a pace every
cycle polls every form (the behaviour before 0.2.4).
"""

from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from typing import Any

from scout.clock import Clock, iso
from scout.config import ScoutConfig
from scout.edgar_parse import AtomEntry, SchemaError, filing_class, parse_getcurrent_atom
from scout.facts import make_fact_id
from scout.health import GOOD_RESULTS, CoverageTracker, board_state, spacing_in_force, status_for
from scout.interfaces import FactSink, Fetcher, HealthSink, RawSink, StatusBoard, TickerMap
from scout.models import Fact
from scout.scheduler import CLOCK_STEP_BACK_S

__all__ = ["SOURCE", "EdgarAtomPoller", "form_url"]

SOURCE = "edgar_atom"


def form_url(template: str, form: str) -> str:
    """The source URL with `{form}` filled in, URL-quoted ("NT 10-Q" -> "NT%2010-Q")."""
    return template.replace("{form}", urllib.parse.quote(form, safe=""))


class EdgarAtomPoller:
    def __init__(self, cfg: ScoutConfig, fetcher: Fetcher, raw: RawSink, facts: FactSink, health: HealthSink,
                 board: StatusBoard, ticker_map: TickerMap, clock: Clock, *, tracker: CoverageTracker | None = None,
                 log: logging.Logger | None = None, between: Callable[[], object] | None = None,
                 pace: Any = None) -> None:
        self.cfg = cfg
        self.src = cfg.source(SOURCE)
        self.fetcher, self.raw, self.facts, self.health, self.board = fetcher, raw, facts, health, board
        self.ticker_map = ticker_map
        self.clock = clock
        self.tracker = tracker or CoverageTracker(cfg, pace=pace)
        self.log = log or logging.getLogger("scout.edgar_atom")
        self.between = between
        self.pace = pace
        self.last_ok: str | None = None
        self.last_error: str | None = None
        # Forms whose last 200 page was not fully read: the fetcher may keep its ETag, so the next
        # request for them goes without validators (0.2.2 review).
        self.refetch_full: set[str] = set()
        self.last_polled: dict[str, datetime] = {}  # the cycle time each form was last polled at (paced)
        self.form_results: dict[str, str] = {}  # each form's latest result

    def spacing(self, form: str, now: datetime) -> float:
        """One form's spacing at `now` (its pace, or the source's fixed spacing)."""
        return spacing_in_force(self.src, now, self.cfg, self.pace, form)

    def interval_at(self, now: datetime) -> float:
        """How often the cycle runs: the fastest form's spacing at `now` (the job's `interval_fn`)."""
        return min((self.spacing(f, now) for f in self.cfg.edgar_atom_forms), default=float(self.src.min_spacing_s))

    def _due(self, form: str, now: datetime) -> bool:
        """Paced: the form's own spacing has passed since the cycle it was last polled in (a clock
        step back of more than `CLOCK_STEP_BACK_S` makes it due, as for any job). Not paced: always."""
        last = self.last_polled.get(form)
        if self.pace is None or last is None:
            return True
        elapsed = (now - last).total_seconds()
        if elapsed < 0:
            return -elapsed > CLOCK_STEP_BACK_S
        return elapsed >= self.spacing(form, now) - 1e-6

    def _publish(self, status: str, now: datetime) -> None:
        self.board.update(SOURCE, board_state(self.src.enabled, status, self.last_ok, self.last_error,
                                              self.tracker.gap_open(SOURCE, now)))

    def _map_ready(self) -> bool:
        ready = getattr(self.ticker_map, "ready", None)
        return bool(ready()) if callable(ready) else True

    def poll(self, now: datetime) -> int:
        """Poll every configured form that is due (every form when not paced); returns the number of
        new facts."""
        if not self.src.enabled:
            self._publish("disabled", now)
            return 0
        if not self._map_ready():
            self.last_error = "the CIK-to-ticker map is not loaded yet"
            self.log.info("edgar_atom: skipped (%s)", self.last_error)
            self._publish("stale", now)
            return 0
        added = polled = 0
        for form in self.cfg.edgar_atom_forms:
            if not self._due(form, now):
                continue
            if polled and self.between is not None:
                self._yield()
            polled += 1
            self.last_polled[form] = now
            try:
                got, result = self._poll_form(form, now)
            except Exception as exc:  # noqa: BLE001 - one form's failure never stops the later forms
                self.last_error = f"{form}: {type(exc).__name__}"
                self.log.warning("edgar_atom %s: poll failed (%s)", form, type(exc).__name__)
                got, result = 0, "network"
            added += got
            self.form_results[form] = result
        latest = [self.form_results[f] for f in self.cfg.edgar_atom_forms if f in self.form_results]
        if polled and all(r in GOOD_RESULTS for r in latest):
            self.last_ok = iso(self.clock.now())
        self._publish(status_for(latest), now)
        return added

    def _yield(self) -> None:
        """Let the due wire polls run between two form requests (never raises)."""
        try:
            self.between()  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - the EDGAR cycle goes on
            self.log.warning("edgar_atom: a job run between forms failed (%s)", type(exc).__name__)

    def _poll_form(self, form: str, now: datetime) -> tuple[int, str]:
        fetch = self.fetcher.fetch(SOURCE, form_url(self.src.url, form), conditional=form not in self.refetch_full)
        if fetch.result == "not_modified":
            self.health.record(self.tracker.observe(self.src, form, fetch, now=now))
            return 0, fetch.result
        if fetch.result != "ok":
            self.last_error = f"{form}: {fetch.error or fetch.result}"
            self.health.record(self.tracker.observe(self.src, form, fetch, now=now))
            return 0, fetch.result
        self.refetch_full.add(form)  # until this page is fully read and recorded
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
        self.refetch_full.discard(form)
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
