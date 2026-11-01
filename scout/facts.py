"""Fact writer (SCOUT_SPEC 6.3, 10.2; DESIGN 2.2, 5.1, D5).

Facts go to `state/facts/<ET date of first_seen_at>.jsonl`, one line each, append-only. A fact is
stored once: `fact_id` = sha256 of "{source}|{guid}|{ticker}" (ticker empty when none), so one
release naming several tickers gives one fact per ticker. Duplicates are found over the last
`fact_dedupe_days` ET days of files (loaded at start; 7 by default) and everything added since: a
quiet EDGAR feed can still list last week's entries after a restart, and those must not be stored
again as new. Facts never depend on an LLM.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from scout.clock import Clock, et_date, parse_iso
from scout.models import Fact
from scout.paths import ScoutPaths
from scout.store import append_jsonl, read_jsonl

__all__ = ["FactWriter", "make_fact_id"]


def make_fact_id(source: str, guid: str, ticker: str | None) -> str:
    return hashlib.sha256(f"{source}|{guid}|{ticker or ''}".encode()).hexdigest()


class FactWriter:
    """implements interfaces.FactSink."""

    def __init__(self, paths: ScoutPaths, clock: Clock, days: int = 7) -> None:
        self.paths = paths
        self.clock = clock
        self.days = max(2, int(days))
        self._seen: dict[str, date] = {}
        today = et_date(clock.now())
        for back in range(self.days - 1, -1, -1):
            day = today - timedelta(days=back)
            for rec in read_jsonl(paths.day_file(paths.facts, day)):
                fid = rec.get("fact_id")
                if isinstance(fid, str):
                    self._seen[fid] = day
        self._pruned_on = today

    def seen(self, fact_id: str) -> bool:
        return fact_id in self._seen

    def add(self, fact: Fact) -> bool:
        """Append `fact` unless its fact_id is stored already. A failed write raises (the poller
        then leaves the item unseen, so the next poll tries again)."""
        if fact.fact_id in self._seen:
            return False
        at = parse_iso(fact.first_seen_at)
        if at is None:
            raise ValueError("fact: first_seen_at is not a UTC time")
        day = et_date(at)
        append_jsonl(self.paths, self.paths.day_file(self.paths.facts, day), fact.to_record())
        self._seen[fact.fact_id] = day
        self._prune(self.clock.now())
        return True

    def _prune(self, now: datetime) -> None:
        today = et_date(now)
        if today == self._pruned_on:
            return
        keep_from = today - timedelta(days=self.days - 1)
        self._seen = {k: d for k, d in self._seen.items() if d >= keep_from}
        self._pruned_on = today
