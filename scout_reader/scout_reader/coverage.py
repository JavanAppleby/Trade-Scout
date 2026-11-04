"""The SCOUT_SPEC 6.2 gap rule over stored health records (DESIGN 3.7, D7). Stdlib only; never raises.

A source is covered over [start, end] only when, for every feed of that source, the chain of good
polls reaches from at or before `start` to within the gap limit of `end`, with no gap between:

- `failed_poll`: a poll whose result is not "ok" / "not_modified", unless the next good poll
  overlaps the good poll before the failure and comes within the interval allowance of it (owner,
  2026-11-04: one timed-out EDGAR poll is not a gap when the next poll overlaps). At most
  `MAX_FORGIVEN` (3) failed polls in a row are forgiven; a longer run of failures stays a gap. A
  failure with no good poll after it yet, or before the first good poll, stays a gap;
- `no_overlap`: a good poll without overlap (`overlap` not true) with the previous good poll;
- `interval`: two good polls further apart than their allowance. The limit is
  max(`max_gap_s`, 2 x S), where S is the larger `spacing_s` of the two good polls (owner,
  2026-11-04: the poll before the 04:00 ET switch from idle to active spacing was scheduled at the
  idle spacing). When 1 to `MAX_FORGIVEN` failed polls lie between them and the later good poll
  overlaps, the allowance is the limit plus each failed poll's own `spacing_s` (each failure used
  up one slot of the schedule, at the spacing it was scheduled at). A run of more than
  `MAX_FORGIVEN` failures is one `failed_poll` gap from the good poll before it to the good poll
  after it;
- `no_poll_at_start`: no good poll at or before `start` (or none at all);
- `no_poll_at_end`: the last good poll is longer than the limit before `end`;
- `missing_feed`: a feed the caller expects (`feeds`, from params `gate6_feeds`) has no record at
  all. Without this, a feed that stopped writing health records more than a day before the window
  would simply vanish and the remaining feeds would count as full coverage.

When `feeds` is given, only those feeds are checked (owner, 2026-11-04): the other feeds of the
source (EDGAR's 10-Q and 10-K, polled for the gate-5 check only) hold no fact that gate 6 or
`entry_blocks` reads. Without `feeds`, every feed of the source is checked.

A poll "observes" at its `start`; a poll whose `end` is after the window's end (or unreadable) is
not used, so an "as of t" answer sees only polls finished by t. Pass the records of the ET days covering [start - 1 day, end] so
the poll before the window is found. Each gap is {"from", "to", "why"} (UTC "...Z"), clipped to the
window. An input that cannot be read gives one gap over the whole window with why "error": fail
closed, never an exception into the caller.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from scout_reader.records import known, parse_time

__all__ = ["GOOD_RESULTS", "MAX_FORGIVEN", "NOT_COVERAGE", "WHY", "Poll", "allowance_s", "chain_gaps", "gaps", "poll_of", "sources_for_gate6"]

#: Beat entries that are never gate coverage: the Discord capture cadence (DESIGN 2.7) is shown on the
#: heartbeat for the owner only.
NOT_COVERAGE = frozenset({"discord_inbox"})

GOOD_RESULTS = frozenset({"ok", "not_modified"})
#: The most failed polls in a row that an overlapping good poll after them forgives (owner, 2026-11-04).
MAX_FORGIVEN = 3
WHY = ("failed_poll", "no_overlap", "interval", "no_poll_at_start", "no_poll_at_end", "missing_feed", "error")


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gap(a: datetime, b: datetime, why: str) -> dict[str, str]:
    return {"from": _z(a), "to": _z(b), "why": why}


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def gaps(health_records: Iterable[Mapping[str, Any]], source: str, start: datetime, end: datetime, *,
         max_gap_s: float, feeds: Sequence[str] | None = None) -> list[dict[str, str]]:
    """Every gap of `source` intersecting [start, end]; [] means covered. `feeds`, when given, are
    the feeds the source must have: one with no record is a `missing_feed` gap over the window."""
    try:
        return _gaps(health_records, source, start, end, float(max_gap_s), feeds)
    except Exception:  # noqa: BLE001 - fail closed, never raise
        try:
            return [_gap(start, end, "error")]
        except Exception:  # noqa: BLE001 - even the window was unreadable
            return [{"from": "", "to": "", "why": "error"}]


def _gaps(records: Iterable[Mapping[str, Any]], source: str, start: datetime, end: datetime,
          max_gap_s: float, expected: Sequence[str] | None = None) -> list[dict[str, str]]:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("naive window")
    if end < start:
        return []
    feeds: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = {}
    for rec in records:
        if not isinstance(rec, Mapping) or not known(rec) or rec.get("source") != source:
            continue
        at, done = parse_time(rec.get("start")), parse_time(rec.get("end"))
        if at is None or at > end or done is None or done > end:
            continue  # no look-ahead: a poll not finished by `end` is not used
        feeds.setdefault(str(rec.get("feed") or ""), []).append((at, rec))
    wanted: set[str] | None = None
    if expected is not None and not isinstance(expected, (str, bytes)):
        wanted = {str(f) for f in expected} or None  # an empty list checks every feed (fail closed)
        missing = [f for f in (wanted or ()) if f not in feeds]
    else:
        missing = []
    if not feeds:
        return [_gap(start, end, "no_poll_at_start")] + ([_gap(start, end, "missing_feed")] if missing else [])
    out: list[dict[str, str]] = [_gap(start, end, "missing_feed") for _f in missing[:1]]
    for name, polls in feeds.items():
        if wanted is not None and name not in wanted:
            continue  # a feed gate 6 does not read (owner, 2026-11-04)
        out.extend(_feed_gaps(sorted(polls, key=lambda p: p[0]), start, end, max_gap_s))
    out.sort(key=lambda g: (g["from"], g["to"], g["why"]))
    unique: list[dict[str, str]] = []
    for g in out:
        if g not in unique:
            unique.append(g)
    return unique


#: One poll as the gap rule sees it: (start, good, overlap, spacing_s). `scout_reader.blocks` keeps
#: these per feed in its cache (with more fields after them), so a long window is not parsed again
#: on every call.
Poll = tuple[datetime, bool, bool, float]


def poll_of(at: datetime, rec: Mapping[str, Any]) -> Poll:
    result = rec.get("result")
    return (at, result in GOOD_RESULTS, result == "not_modified" or rec.get("overlap") is True,
            _number(rec.get("spacing_s"), 0.0))


def _feed_gaps(polls: list[tuple[datetime, Mapping[str, Any]]], start: datetime, end: datetime,
               max_gap_s: float) -> list[dict[str, str]]:
    return chain_gaps([poll_of(at, rec) for at, rec in polls], start, end, max_gap_s)


def allowance_s(max_gap_s: float, spacing_before: float, spacing_after: float,
                failed_spacings: Sequence[float], overlap: bool) -> float:
    """The longest interval between two good polls that is not a gap (SS 6.2): max(`max_gap_s`, 2 x S)
    with S the larger spacing of the two polls, plus the `spacing_s` of each failed poll between
    them when there are 1 to `MAX_FORGIVEN` of them and the later poll overlaps. `scout.health` uses
    the same rule."""
    s = max(float(spacing_before), float(spacing_after))
    limit = max(float(max_gap_s), 2.0 * s)
    if overlap and 0 < len(failed_spacings) <= MAX_FORGIVEN:
        limit += sum(float(f) for f in failed_spacings)
    return limit


def chain_gaps(chain: Sequence[tuple[Any, ...]], start: datetime, end: datetime, max_gap_s: float) -> list[dict[str, str]]:
    """The gaps of one feed's polls (sorted by start; each a `Poll`, or a longer tuple that starts
    with one) intersecting [start, end]. Polls after `end`
    are not used. The walk starts at the last good poll strictly before `start` (or at the first
    poll): every gap that ends before it lies outside the window, so the answer is the same as a
    walk over the whole chain."""
    stop = bisect.bisect_right(chain, end, key=lambda p: p[0])
    first = bisect.bisect_left(chain, start, 0, stop, key=lambda p: p[0]) - 1
    while first >= 0 and not chain[first][1]:
        first -= 1
    polls = chain[max(first, 0):stop]
    raw: list[tuple[datetime, datetime, str]] = []
    anchor: datetime | None = None  # the last good poll
    anchor_spacing = 0.0
    failed: list[tuple[datetime, datetime, str]] = []  # failures since `anchor`, not yet forgiven
    failed_spacings: list[float] = []  # their own spacing_s
    limit = max_gap_s
    for p in polls:
        at, good, overlap, spacing = p[0], p[1], p[2], p[3]
        limit = max(max_gap_s, 2.0 * spacing)
        if not good:
            if anchor is not None:
                failed.append((anchor, at, "failed_poll"))
                failed_spacings.append(spacing)
            else:
                raw.append((at, at, "failed_poll"))
            continue
        if anchor is None:
            pass  # the first good poll: what came before it is `no_poll_at_start`
        elif not overlap:
            raw.append((anchor, at, "no_overlap"))
            raw.extend(failed)
        elif (at - anchor).total_seconds() > allowance_s(max_gap_s, anchor_spacing, spacing, failed_spacings, True):
            raw.append((anchor, at, "interval"))
            raw.extend(failed)
        elif len(failed) > MAX_FORGIVEN:
            raw.extend(failed)  # too many failures in a row: they stay gaps,
            raw.append((anchor, at, "failed_poll"))  # up to the good poll after them
        # else: the good poll overlaps the one before the failures, in time: nothing was missed
        failed, failed_spacings = [], []
        anchor, anchor_spacing = at, spacing
    raw.extend(failed)  # no good poll after them (yet)
    first_good = next((p[0] for p in polls if p[1]), None)
    if first_good is None or first_good > start:
        raw.append((start, first_good or end, "no_poll_at_start"))
    if anchor is None or (end - anchor).total_seconds() > limit:
        raw.append((anchor or start, end, "no_poll_at_end"))
    out: list[dict[str, str]] = []
    for a, b, why in raw:
        if b < start or a > end:
            continue
        out.append(_gap(max(a, start), min(b, end), why))
    return out


def sources_for_gate6(beat: Mapping[str, Any] | None, params: Mapping[str, Any] | None = None) -> list[str]:
    """The sources whose coverage gate 6 needs: `params["gate6_sources"]` when readable; otherwise
    every source the beat lists that is not "disabled" (more sources to check can only turn an
    answer into unknown, never into a wrong false)."""
    try:
        if params is not None:
            listed = params.get("gate6_sources")
            if isinstance(listed, list) and all(isinstance(s, str) for s in listed):
                return sorted(set(listed))
        sources = (beat or {}).get("sources")
        if isinstance(sources, Mapping):
            return sorted(str(n) for n, status in sources.items()
                          if status != "disabled" and str(n) not in NOT_COVERAGE)
    except Exception:  # noqa: BLE001 - never raise
        pass
    return []
