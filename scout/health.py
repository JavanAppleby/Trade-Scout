"""Health records and coverage (SCOUT_SPEC 6.1, 6.2; DESIGN 2.2, 5.2, 7, D7).

`HealthWriter` appends one record per poll to `state/health/<ET date of start>.jsonl`.

`CoverageTracker` keeps, per source and feed, the guids already seen (at most `seen_guid_cap`) and
the last good poll, and builds each poll's record:
- `overlap` (SS 6.2): the poll contains a guid already seen, or returns fewer items than the feed's
  maximum (`max_items`; 0 = unknown, so only a seen guid counts). A 304 counts as overlap, unless
  a page was lost since the last good poll (see `seen_items`). A failed poll has `overlap = null`.
- `seen_items` (0.2.2): how many distinct ids of a good page Scout had already seen in this feed
  and that sit below every new id on the page (a feed lists newest first, so only a seen id below
  the new ones shows the page reached back past them; a seen id re-listed above a new one proves
  nothing). A 304 counts as overlap by a seen id, unless a page was received and not read since
  the last good poll (`scout_reader.coverage.page_lost`): then the 304 may rest on that lost page's
  ETag, so it has no overlap. Null for a failed poll.
- `gap_before` (D7): the interval since the last good poll of this feed is a gap: the poll failed,
  it has no overlap, or `scout_reader.coverage.interval_gap` finds one. That is the reader's own
  rule: a poll that repeats a seen id covers the interval, failed polls included, up to
  `source_overlap_max_gap_s` (owner, 2026-11-04); otherwise (a short page only, or beyond that cap)
  more than `MAX_FORGIVEN` (3) failed polls, or an interval longer than
  `scout_reader.coverage.allowance_s`: max(`source_max_gap_s`, 2 x S) with S the larger spacing of
  this poll and the last good poll, plus each failed poll's own spacing (1 to 3 of them), is a gap.
  With no earlier good poll at all, an overlapping poll has no gap before it unless polls failed
  before it (the reader counts those failures as gaps).
- `spacing_in_force`: a source's spacing at a time: `min_spacing_s` inside `active_hours_et`,
  `idle_spacing_s` outside, and for `hour_turn_sources` at most `hour_turn_spacing_s` from minute
  `hour_turn_start_min` to `hour_turn_end_min` ET around each hour inside active hours (owner,
  2026-11-04). The scheduler polls at this spacing and each health record stores it.
- a seed (`scout.coverage_seed`, owner 2026-11-04): at start, each feed's seen ids, last good poll
  and failed polls since come from Scout's own stored facts and health records, so the first poll
  after a restart is judged like any other. No seed (or a broken one): the feed starts empty, as
  before 0.2.1.
- `gap_open(source)`: the latest poll of some gate-6 feed left a gap, or (given `now`) its last good
  poll is older than that limit. Periodic forms (10-Q, 10-K, ...: polled for the gate-5 check only,
  not in params `gate6_feeds`) do not count. The heartbeat publishes it.
Readers do not trust these flags alone: `scout_reader.coverage.gaps` recomputes gaps from the stored
records.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from scout.calendar import in_active_hours
from scout.clock import et_date, parse_iso, to_et
from scout.config import PERIODIC_FORMS, ScoutConfig, SourceConfig
from scout.http import host_spacing_s
from scout.models import FetchResult, HealthRecord, SourceState
from scout.paths import ScoutPaths
from scout.store import append_jsonl
from scout_reader.coverage import interval_gap, page_lost

if TYPE_CHECKING:  # pragma: no cover
    from scout.coverage_seed import FeedSeed

__all__ = ["GOOD_RESULTS", "CoverageTracker", "HealthWriter", "board_state", "gap_limit_s", "in_hour_turn",
           "spacing_in_force", "status_for"]

GOOD_RESULTS = frozenset({"ok", "not_modified"})
_UNAVAILABLE = frozenset({"schema", "no_http", "too_large", "host_cap"})


class HealthWriter:
    """implements interfaces.HealthSink."""

    def __init__(self, paths: ScoutPaths) -> None:
        self.paths = paths

    def record(self, rec: HealthRecord) -> None:
        start = parse_iso(rec.start)
        if start is None:
            raise ValueError("health: start is not a UTC time")
        append_jsonl(self.paths, self.paths.day_file(self.paths.health, et_date(start)), rec.to_record())


def in_hour_turn(now: datetime, cfg: ScoutConfig) -> bool:
    """True from minute `hour_turn_start_min` to `hour_turn_end_min` (not included) ET around each
    hour, inside `active_hours_et` (owner, 2026-11-04)."""
    if not in_active_hours(now, cfg):
        return False
    minute, start, end = to_et(now).minute, int(cfg.hour_turn_start_min), int(cfg.hour_turn_end_min)
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # the window wraps the hour, e.g. :58 to :05


def spacing_in_force(src: SourceConfig, now: datetime, cfg: ScoutConfig) -> float:
    """`min_spacing_s` inside `active_hours_et`, `idle_spacing_s` outside (0 = the same); for a
    source in `hour_turn_sources`, at most `hour_turn_spacing_s` around the hour (`in_hour_turn`).
    Validation keeps `hour_turn_spacing_s` at or above the host's hard minimum (scout.http); a
    larger configured `host_min_spacing_s` for the source's host also bounds it here, so the job
    never polls faster than the fetcher sends (it would sleep inside the one loop) and each health
    record stores the spacing really kept (0.2.2 review)."""
    if src.idle_spacing_s and not in_active_hours(now, cfg):
        return float(src.idle_spacing_s)
    if src.name in cfg.hour_turn_sources and in_hour_turn(now, cfg):
        kept = max((host_spacing_s(cfg, h) for h in src.allowed_hosts), default=0.0)
        return min(float(src.min_spacing_s), max(float(cfg.hour_turn_spacing_s), kept))
    return float(src.min_spacing_s)


def gap_limit_s(cfg: ScoutConfig, spacing_s: float) -> float:
    """D7: an interval longer than this between good polls is a gap."""
    return max(float(cfg.source_max_gap_s), 2.0 * float(spacing_s))


def status_for(results: Sequence[str]) -> str:
    """A source's status from this poll's per-feed results."""
    if not results:
        return "starting"
    if all(r in GOOD_RESULTS for r in results):
        return "ok"
    if "refused" in results:
        return "refused"
    if any(r in _UNAVAILABLE for r in results):
        return "unavailable"
    return "stale"


def board_state(enabled: bool, status: str, last_ok: str | None, last_error: str | None,
                gap_open: bool) -> SourceState:
    return SourceState(enabled=enabled, status=status if enabled else "disabled", last_ok=last_ok,
                       last_error=last_error, gap_open=gap_open)


class _Feed:
    def __init__(self) -> None:
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.anchor: datetime | None = None  # start of the last good poll
        self.anchor_spacing_s: float = 0.0  # its spacing
        self.failed: list[float] = []  # the spacing of each failed poll since the anchor
        self.lost = False  # a page received and not read since the anchor (a 304 then proves nothing)
        self.spacing_s: float = 0.0  # the spacing of the latest poll
        self.last: HealthRecord | None = None


class CoverageTracker:
    def __init__(self, cfg: ScoutConfig, seeds: Mapping[tuple[str, str], FeedSeed] | None = None) -> None:
        self.cfg = cfg
        self._feeds: dict[tuple[str, str], _Feed] = {}
        self._seeds = dict(seeds or {})

    def _seeded(self, key: tuple[str, str]) -> _Feed:
        """A feed's state at its first use: its seed, if any (`last` stays None: the board shows a
        gap until this run has polled the feed)."""
        state = _Feed()
        seed = self._seeds.pop(key, None)
        if seed is not None:
            for g in seed.guids[-self.cfg.seen_guid_cap:]:
                state.seen[g] = None
            state.anchor, state.anchor_spacing_s = seed.anchor, seed.spacing_s
            state.spacing_s, state.failed = seed.spacing_s, list(seed.failed_spacings)
            state.lost = seed.lost_page
        return state

    def _feed(self, source: str, feed: str) -> _Feed:
        key = (source, feed)
        if key not in self._feeds:
            self._feeds[key] = self._seeded(key)
        return self._feeds[key]

    def feeds(self, source: str) -> list[str]:
        return [f for (s, f) in self._feeds if s == source]

    def new_guids(self, source: str, feed: str, guids: Iterable[str]) -> list[str]:
        """The guids not seen before in this feed, in order, without repeats."""
        seen = self._feed(source, feed).seen
        return [g for g in dict.fromkeys(guids) if g not in seen]

    def observe(self, src: SourceConfig, feed: str, fetch: FetchResult, *, now: datetime,
                result: str | None = None, guids: Sequence[str] = (), new_items: int = 0,
                page_size: int | None = None, error: str | None = None) -> HealthRecord:
        """Build this poll's health record and update the feed's state. `result` overrides the
        fetch result (a parse failure is "schema"); `guids` are every item of a good poll;
        `page_size` is the number of entries on the page when it differs from len(guids)."""
        state = self._feed(src.name, feed)
        outcome = result or fetch.result
        spacing = spacing_in_force(src, now, self.cfg)
        start = parse_iso(fetch.started_at) or now
        good = outcome in GOOD_RESULTS
        overlap: bool | None = None
        seen_items: int | None = None
        if outcome == "not_modified":
            overlap, seen_items = not state.lost, 0  # after a lost page: that page's ETag, not Scout's
        elif good:
            unique = list(dict.fromkeys(guids))
            last_new = max((i for i, g in enumerate(unique) if g not in state.seen), default=-1)
            seen_items = sum(1 for g in unique[last_new + 1:] if g in state.seen)  # below every new id
            size = page_size if page_size is not None else len(unique)
            short = src.max_items > 0 and size < src.max_items
            overlap = any(g in state.seen for g in unique) or short  # SS 6.2 as in 0.2.1
        seen = bool(overlap) and (outcome == "not_modified" or bool(seen_items))
        if not good or not overlap:
            gap_before = True
        elif state.anchor is None:
            gap_before = bool(state.failed)  # the reader counts failures before any good poll as gaps
        else:  # the reader's rule (scout_reader.coverage.interval_gap), on the same numbers
            gap_before = interval_gap((start - state.anchor).total_seconds(), self.cfg.source_max_gap_s,
                                      state.anchor_spacing_s, spacing, state.failed, seen,
                                      self.cfg.source_overlap_max_gap_s) is not None
        rec = HealthRecord(
            source=src.name, feed=feed, kind=src.kind, enabled=src.enabled, gate6=src.gate6,
            start=fetch.started_at, end=fetch.ended_at, result=outcome, http_status=fetch.status,
            items=len(guids) if good else 0, new_items=new_items if good else 0, overlap=overlap,
            spacing_s=spacing, gap_before=gap_before, error=error if error is not None else fetch.error,
            seen_items=seen_items)
        if good:
            for g in guids:
                state.seen[g] = None
                state.seen.move_to_end(g)
            while len(state.seen) > self.cfg.seen_guid_cap:
                state.seen.popitem(last=False)
            state.anchor, state.anchor_spacing_s, state.failed, state.lost = start, spacing, [], False
        else:
            state.failed.append(spacing)
            state.lost = state.lost or page_lost(outcome, fetch.status)
        state.spacing_s = spacing
        state.last = rec
        return rec

    def gap_open(self, source: str, now: datetime | None = None) -> bool:
        feeds = [self._feeds[k] for k in self._feeds if k[0] == source and k[1].upper() not in PERIODIC_FORMS]
        if not feeds:
            return True
        for state in feeds:
            if state.last is None or state.last.gap_before or state.last.result not in GOOD_RESULTS:
                return True
            if now is not None and state.anchor is not None and \
                    (now - state.anchor).total_seconds() > gap_limit_s(self.cfg, state.spacing_s):
                return True
        return False
