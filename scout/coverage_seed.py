"""Coverage across a restart (SCOUT_SPEC 6.2; DESIGN 5.2, D7; owner, 2026-11-04).

`CoverageTracker` keeps each feed's seen item ids and its last good poll in memory. Without a seed,
the first poll of every feed after a restart had nothing to overlap with, so it was a `no_overlap`
gap, and every gate-6 answer stayed `unknown` for 3 trading days after each restart. The seed lets
that first poll be judged by the same overlap and interval rules as any other poll.

`load_seeds` reads back only what Scout itself stored before `now`, over the last `days` ET days:

- the anchor: the last good health record of each feed (its `start` and `spacing_s`), and the
  number of failed polls recorded after it;
- the seen ids: the `guid` of every fact stored by a good poll of that feed. Every new item of a
  good EDGAR or wire page becomes at least one fact (one without a ticker when none is found), and
  a fact's `first_seen_at` is its poll's `end`, so a fact belongs to a good poll exactly when its
  (source, feed, first_seen_at) matches a good health record's (source, feed, end). Facts written by
  a poll that then failed have no such health record and are not used: that poll's page was not
  fully handled. At most `cap` (`seen_guid_cap`) ids per feed are kept, the newest.

A feed without a readable good health record gets no seed. Missing or corrupt files give fewer ids
or no seed, so the tracker claims no overlap it cannot show (fail closed, as before 0.2.1). An id
first stored under another feed of the same source is not in this feed's seed (the fact store keeps
one fact per id): that too can only give fewer overlaps. The interval rule still runs from the
anchor, so a long outage stays an `interval` gap.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scout.clock import et_date
from scout.paths import ScoutPaths
from scout_reader.records import known, parse_time, read_jsonl

__all__ = ["FeedSeed", "load_seeds"]

_GOOD = frozenset({"ok", "not_modified"})

log = logging.getLogger("scout.coverage_seed")


@dataclass(frozen=True)
class FeedSeed:
    """One feed as Scout last stored it: seen ids (oldest first), the start and spacing of the last
    good poll, and the number of failed polls recorded after it."""

    guids: tuple[str, ...]
    anchor: datetime
    spacing_s: float
    failed: int = 0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _records(folder: Path, paths: ScoutPaths, now: datetime, days: int) -> Iterable[Mapping[str, Any]]:
    today = et_date(now)
    for back in range(max(1, days) - 1, -1, -1):
        yield from read_jsonl(paths.day_file(folder, today - timedelta(days=back)))


def load_seeds(paths: ScoutPaths, now: datetime, *, sources: Iterable[str], cap: int,
               days: int) -> dict[tuple[str, str], FeedSeed]:
    """The seed of every (source, feed) of `sources` with a good health record before `now`.
    Never raises: an unreadable store gives {} (no seed, fail closed)."""
    try:
        return _load(paths, now, frozenset(sources), max(1, int(cap)), max(1, int(days)))
    except Exception as exc:  # noqa: BLE001 - a broken seed only means no seed
        log.warning("coverage seed not loaded (%s); first polls claim no overlap", type(exc).__name__)
        return {}


def _load(paths: ScoutPaths, now: datetime, sources: frozenset[str], cap: int,
          days: int) -> dict[tuple[str, str], FeedSeed]:
    polls: dict[tuple[str, str], list[tuple[datetime, bool, float | None]]] = {}
    good_ends: set[tuple[str, str, datetime]] = set()
    for rec in _records(paths.health, paths, now, days):
        source, feed = rec.get("source"), rec.get("feed")
        if not known(rec) or source not in sources or not isinstance(feed, str):
            continue
        at, end = parse_time(rec.get("start")), parse_time(rec.get("end"))
        if at is None or at > now:
            continue  # no look-ahead: only polls Scout made before now
        good = rec.get("result") in _GOOD
        polls.setdefault((str(source), feed), []).append((at, good, _number(rec.get("spacing_s"))))
        if good and end is not None and end <= now:
            good_ends.add((str(source), feed, end))
    anchors: dict[tuple[str, str], tuple[datetime, float, int]] = {}
    for key, items in polls.items():
        items.sort(key=lambda p: p[0])
        last = max((i for i, p in enumerate(items) if p[1]), default=None)
        if last is None:
            continue
        at, _good, spacing = items[last]
        if spacing is None or spacing < 0:
            continue  # an unreadable spacing: no seed for this feed
        anchors[key] = (at, spacing, len(items) - 1 - last)
    seen: dict[tuple[str, str], list[tuple[datetime, str]]] = {k: [] for k in anchors}
    for rec in _records(paths.facts, paths, now, days):
        source, feed, guid = rec.get("source"), rec.get("feed"), rec.get("guid")
        if not known(rec) or not isinstance(source, str) or not isinstance(feed, str) or not isinstance(guid, str):
            continue
        key = (source, feed)
        if key not in seen:
            continue
        at = parse_time(rec.get("first_seen_at"))
        if at is None or at > now or (source, feed, at) not in good_ends:
            continue
        seen[key].append((at, guid))
    out: dict[tuple[str, str], FeedSeed] = {}
    for key, (at, spacing, failed) in anchors.items():
        ordered = dict.fromkeys(g for _t, g in sorted(seen[key], key=lambda p: p[0]))
        out[key] = FeedSeed(guids=tuple(list(ordered)[-cap:]), anchor=at, spacing_s=spacing, failed=failed)
    return out
