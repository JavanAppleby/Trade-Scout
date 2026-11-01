"""Health records and coverage (SCOUT_SPEC 6.1, 6.2; DESIGN 2.2, 5.2, 7, D7).

`HealthWriter` appends one record per poll to `state/health/<ET date of start>.jsonl`.

`CoverageTracker` keeps, per source and feed, the guids already seen (at most `seen_guid_cap`) and
the last good poll, and builds each poll's record:
- `overlap` (SS 6.2): the poll contains a guid already seen, or returns fewer items than the feed's
  maximum (`max_items`; 0 = unknown, so only a seen guid counts). A 304 counts as overlap. A failed
  poll has `overlap = null`.
- `gap_before` (D7): the interval since the last good poll of this feed is a gap: the poll failed,
  it has no overlap, there was no earlier good poll in this run (unless the poll overlaps), or the
  interval is longer than max(`source_max_gap_s`, 2 x the spacing in force).
- `gap_open(source)`: the latest poll of some feed left a gap, or (given `now`) the last good poll
  is older than that limit. The heartbeat publishes it.
Readers do not trust these flags alone: `scout_reader.coverage.gaps` recomputes gaps from the stored
records.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Sequence
from datetime import datetime

from scout.calendar import in_active_hours
from scout.clock import et_date, parse_iso
from scout.config import ScoutConfig, SourceConfig
from scout.models import FetchResult, HealthRecord, SourceState
from scout.paths import ScoutPaths
from scout.store import append_jsonl

__all__ = ["GOOD_RESULTS", "CoverageTracker", "HealthWriter", "board_state", "gap_limit_s", "spacing_in_force",
           "status_for"]

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


def spacing_in_force(src: SourceConfig, now: datetime, cfg: ScoutConfig) -> float:
    """`min_spacing_s` inside `active_hours_et`, `idle_spacing_s` outside (0 = the same)."""
    if src.idle_spacing_s and not in_active_hours(now, cfg):
        return float(src.idle_spacing_s)
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
        self.spacing_s: float = 0.0
        self.last: HealthRecord | None = None


class CoverageTracker:
    def __init__(self, cfg: ScoutConfig) -> None:
        self.cfg = cfg
        self._feeds: dict[tuple[str, str], _Feed] = {}

    def _feed(self, source: str, feed: str) -> _Feed:
        return self._feeds.setdefault((source, feed), _Feed())

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
        if outcome == "not_modified":
            overlap = True
        elif good:
            unique = list(dict.fromkeys(guids))
            seen_one = any(g in state.seen for g in unique)
            size = page_size if page_size is not None else len(unique)
            short = src.max_items > 0 and size < src.max_items
            overlap = seen_one or short
        if not good or not overlap:
            gap_before = True
        elif state.anchor is None:
            gap_before = False  # the feed holds fewer items than it can: nothing passed unseen
        else:
            gap_before = (start - state.anchor).total_seconds() > gap_limit_s(self.cfg, spacing)
        rec = HealthRecord(
            source=src.name, feed=feed, kind=src.kind, enabled=src.enabled, gate6=src.gate6,
            start=fetch.started_at, end=fetch.ended_at, result=outcome, http_status=fetch.status,
            items=len(guids) if good else 0, new_items=new_items if good else 0, overlap=overlap,
            spacing_s=spacing, gap_before=gap_before, error=error if error is not None else fetch.error)
        if good:
            for g in guids:
                state.seen[g] = None
                state.seen.move_to_end(g)
            while len(state.seen) > self.cfg.seen_guid_cap:
                state.seen.popitem(last=False)
            state.anchor = start
        state.spacing_s = spacing
        state.last = rec
        return rec

    def gap_open(self, source: str, now: datetime | None = None) -> bool:
        feeds = [self._feeds[k] for k in self._feeds if k[0] == source]
        if not feeds:
            return True
        for state in feeds:
            if state.last is None or state.last.gap_before or state.last.result not in GOOD_RESULTS:
                return True
            if now is not None and state.anchor is not None and \
                    (now - state.anchor).total_seconds() > gap_limit_s(self.cfg, state.spacing_s):
                return True
        return False
