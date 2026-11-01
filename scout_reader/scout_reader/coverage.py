"""The SCOUT_SPEC 6.2 gap rule over stored health records (DESIGN 3.7, D7). Stdlib only; never raises.

A source is covered over [start, end] only when, for every feed of that source, the chain of good
polls reaches from at or before `start` to within the gap limit of `end`, with no gap between:

- `failed_poll`: a poll whose result is not "ok" / "not_modified" (any failed poll is a gap, SS 6.2);
- `no_overlap`: a good poll without overlap (`overlap` not true) with the previous good poll;
- `interval`: more than max(`max_gap_s`, 2 x the record's `spacing_s`) between two good polls;
- `no_poll_at_start`: no good poll at or before `start` (or none at all);
- `no_poll_at_end`: the last good poll is longer than the limit before `end`;
- `missing_feed`: a feed the caller expects (`feeds`, from params `gate6_feeds`) has no record at
  all. Without this, a feed that stopped writing health records more than a day before the window
  would simply vanish and the remaining feeds would count as full coverage.

A poll "observes" at its `start`. Pass the records of the ET days covering [start - 1 day, end] so
the poll before the window is found. Each gap is {"from", "to", "why"} (UTC "...Z"), clipped to the
window. An input that cannot be read gives one gap over the whole window with why "error": fail
closed, never an exception into the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from scout_reader.records import known, parse_time

__all__ = ["GOOD_RESULTS", "WHY", "gaps", "sources_for_gate6"]

GOOD_RESULTS = frozenset({"ok", "not_modified"})
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
        at = parse_time(rec.get("start"))
        if at is None or at > end:
            continue
        feeds.setdefault(str(rec.get("feed") or ""), []).append((at, rec))
    if expected is not None and not isinstance(expected, (str, bytes)):
        missing = [str(f) for f in expected if str(f) not in feeds]
    else:
        missing = []
    if not feeds:
        return [_gap(start, end, "no_poll_at_start")] + ([_gap(start, end, "missing_feed")] if missing else [])
    out: list[dict[str, str]] = [_gap(start, end, "missing_feed") for _f in missing[:1]]
    for polls in feeds.values():
        out.extend(_feed_gaps(sorted(polls, key=lambda p: p[0]), start, end, max_gap_s))
    out.sort(key=lambda g: (g["from"], g["to"], g["why"]))
    unique: list[dict[str, str]] = []
    for g in out:
        if g not in unique:
            unique.append(g)
    return unique


def _feed_gaps(polls: list[tuple[datetime, Mapping[str, Any]]], start: datetime, end: datetime,
               max_gap_s: float) -> list[dict[str, str]]:
    raw: list[tuple[datetime, datetime, str]] = []
    anchor: datetime | None = None  # the last good poll
    limit = max_gap_s
    for at, rec in polls:
        limit = max(max_gap_s, 2.0 * _number(rec.get("spacing_s"), 0.0))
        good = rec.get("result") in GOOD_RESULTS
        if not good:
            raw.append((anchor or at, at, "failed_poll"))
            continue
        overlap = rec.get("result") == "not_modified" or rec.get("overlap") is True
        if anchor is None:
            pass  # the first good poll: what came before it is `no_poll_at_start`
        elif not overlap:
            raw.append((anchor, at, "no_overlap"))
        elif (at - anchor).total_seconds() > limit:
            raw.append((anchor, at, "interval"))
        anchor = at
    first_good = next((at for at, rec in polls if rec.get("result") in GOOD_RESULTS), None)
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
            return sorted(str(n) for n, status in sources.items() if status != "disabled")
    except Exception:  # noqa: BLE001 - never raise
        pass
    return []
