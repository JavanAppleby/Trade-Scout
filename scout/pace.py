"""Polling pace (owner, 2026-11-10: "Slow Scout down by a factor based on how frequent each source has
updated data - and continue to monitor and adjust timings as time goes on"; DESIGN 21).

Each paced feed (a wire's "rss", or one EDGAR form) gets its own spacing for each ET hour of a trading
day (slots 0-23) and one for closed days (slot 24: weekends and market holidays), measured from
Scout's own health records over the last `pace.lookback_days` ET days:

- gap: the slot's polled time (the summed `spacing_s` of its good polls) divided by the good polls that
  found something new (`new_items` > 0), i.e. the typical time between new items as Scout saw them.
  With none the gap is unbounded, so the slowest allowed spacing applies.
- page guard (feeds with a page size, `max_items` > 0): `busiest(S)` is the most new items Scout saw
  within any S seconds ending at a poll of the slot (each poll's items counted at its time). The
  spacing is the largest one, at or below `factor` x gap, at which `busiest` stays within `page_fill`
  x `max_items`, so a burst like the busiest one seen fills at most that share of the page and nothing
  falls off it unseen (PR Newswire keeps 20 items; 20 releases can come within a minute on the hour),
  while an hour with one release now and then is not held back by it.
- wanted = that spacing, rounded down (10 s steps below 10 minutes, whole minutes above). A slot
  polled for less than `min_hours` in the lookback has no wanted spacing.

At run time (`Pace.spacing`) the wanted spacing is clamped: never faster than the source's fixed
spacing (`min_spacing_s` inside `active_hours_et`, `idle_spacing_s` outside), never slower than the
feed's `[pace.slowest_active_s]` / `[pace.slowest_idle_s]` line. A feed with no line, a slot with no
wanted spacing, no saved plan or `pace.enabled = false` keeps the fixed spacing (Scout before 0.2.4).
The halt feed is never paced: a halt block lasts only as long as the halt.

Why per hour: EDGAR releases nothing new from about 18:00 to 06:00 ET (later filings are disseminated
at 06:00), the wires slow down at night and halts cluster in regular hours; one figure a day would hide
that. The plan is recalculated daily at `pace.recalc_time_et` (03:30 ET) and at start when the saved one
is more than a day old or was made with other settings, and appended to `state/pace/<ET date>.jsonl`,
so a restart keeps it. Only polls that ended by the recalculation are read (no look-ahead). Nothing
here raises into the poll loop: a plan that cannot be read or made leaves the fixed spacing in force.
"""

from __future__ import annotations

import bisect
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from scout import FORMAT
from scout.calendar import in_active_hours, is_trading_day
from scout.clock import et_date, iso, parse_iso, to_et
from scout.config import ScoutConfig, SourceConfig
from scout.paths import ScoutPaths
from scout.store import append_jsonl, read_jsonl

__all__ = ["CLOSED", "SLOTS", "WIRE_FEED", "FeedPlan", "FeedPolls", "Pace", "PacePlan", "SlotStats", "build_plan",
           "clamp", "feeds_of", "fixed_spacing", "load_latest", "measure", "requests_per_day", "round_down", "slot_of",
           "wanted_spacing"]

SLOTS = 25  # ET hours 0-23 of a trading day, then one slot for every closed day
CLOSED = 24
WIRE_FEED = "rss"  # scout.wire_rss.FEED (a test keeps them equal)
NO_NEWS_S = 86400.0  # the wanted spacing of a slot that saw nothing new: the slowest allowed then applies
STALE_AFTER_S = 26 * 3600.0  # a saved plan older than this is recalculated at start
MAX_FILES = 31  # newest `state/pace` files searched for the latest plan
#: The spacings the page guard tries (`round_down`'s steps): 10 s steps to 10 minutes, then minutes to an hour.
GRID_S = tuple(float(s) for s in (*range(10, 600, 10), *range(600, 3601, 60)))
_GOOD = frozenset({"ok", "not_modified"})


def slot_of(at: datetime) -> int:
    """The ET hour of a trading day (0-23), or `CLOSED` on a weekend or market holiday."""
    local = to_et(at)
    return local.hour if is_trading_day(local.date()) else CLOSED


def feeds_of(src: SourceConfig, cfg: ScoutConfig) -> tuple[str, ...]:
    """The feeds a source's health records name: each EDGAR form, or the wire's one feed."""
    return tuple(cfg.edgar_atom_forms) if src.kind == "filing" else (WIRE_FEED,)


def fixed_spacing(src: SourceConfig, active: bool) -> float:
    """The source's own spacing: `min_spacing_s` inside active hours, `idle_spacing_s` (0 = the same) outside."""
    return float(src.min_spacing_s if active or not src.idle_spacing_s else src.idle_spacing_s)


def clamp(wanted: float | None, fixed: float, slowest: float | None) -> float:
    """The spacing in force: `wanted` kept between the fixed spacing and the slowest allowed; the fixed
    spacing when the feed is not paced (`slowest` None) or the slot was not measured (`wanted` None)."""
    if wanted is None or slowest is None:
        return float(fixed)
    return max(float(fixed), min(float(wanted), float(slowest)))


def round_down(seconds: float) -> float:
    """10 s steps below 10 minutes, whole minutes above (never 0)."""
    if seconds < 10.0:
        return max(1.0, float(math.floor(seconds)))
    step = 10.0 if seconds < 600.0 else 60.0
    return math.floor(seconds / step) * step


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and \
        math.isfinite(value) and value > 0 else 0.0


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# --------------------------------------------------------------------------- measuring


@dataclass
class SlotStats:
    """One feed's good polls per slot over the lookback: polled time, polls, polls with new items, and
    the most new items one poll had."""

    polled_s: list[float]
    polls: list[int]
    hits: list[int]
    max_new: list[int]

    @classmethod
    def empty(cls) -> SlotStats:
        return cls([0.0] * SLOTS, [0] * SLOTS, [0] * SLOTS, [0] * SLOTS)


@dataclass
class FeedPolls:
    """One feed's good polls in time order (built by `measure`), for the page guard's windows."""

    times: list[float]  # poll start, POSIX seconds
    cum: list[int]  # cum[i]: new items of polls 0..i-1 (len(times) + 1 values)
    in_slot: list[list[int]]  # per slot, the indexes of its polls
    stats: SlotStats

    @classmethod
    def empty(cls) -> FeedPolls:
        return cls([], [0], [[] for _ in range(SLOTS)], SlotStats.empty())

    def busiest(self, slot: int, window_s: float) -> int:
        """The most new items within `window_s` seconds ending at one of the slot's polls."""
        most = 0
        for j in self.in_slot[slot]:
            first = bisect.bisect_right(self.times, self.times[j] - window_s)
            most = max(most, self.cum[j + 1] - self.cum[first])
        return most


def measure(records: Iterable[Mapping[str, Any]], start: datetime, end: datetime,
            sources: Iterable[str]) -> dict[tuple[str, str], FeedPolls]:
    """Per (source, feed): its good polls in time order with each slot's polled time, polls, polls with
    new items and largest single poll. Polls that started before `start` or ended after `end` are not
    used (no look-ahead); failed polls are not counted."""
    wanted = set(sources)
    by_feed: dict[tuple[str, str], list[tuple[datetime, Mapping[str, Any]]]] = {}
    for rec in records:
        if not isinstance(rec, Mapping) or rec.get("format") != FORMAT or rec.get("source") not in wanted:
            continue
        if rec.get("result") not in _GOOD:
            continue
        at, done = parse_iso(rec.get("start")), parse_iso(rec.get("end"))
        if at is None or done is None or at < start or done > end:
            continue
        by_feed.setdefault((str(rec["source"]), str(rec.get("feed") or "")), []).append((at, rec))
    out: dict[tuple[str, str], FeedPolls] = {}
    for key, rows in by_feed.items():
        rows.sort(key=lambda row: row[0])
        polls = FeedPolls.empty()
        st = polls.stats
        for at, rec in rows:
            slot, spacing, new = slot_of(at), _number(rec.get("spacing_s")), _count(rec.get("new_items"))
            polls.in_slot[slot].append(len(polls.times))
            polls.times.append(at.timestamp())
            polls.cum.append(polls.cum[-1] + new)
            st.polls[slot] += 1
            st.polled_s[slot] += spacing
            if new:
                st.hits[slot] += 1
                st.max_new[slot] = max(st.max_new[slot], new)
        out[key] = polls
    return out


def wanted_spacing(polls: FeedPolls, slot: int, max_items: int, factor: float, page_fill: float,
                   min_hours: float) -> float | None:
    """The slot's wanted spacing (see the module doc), or None when it was polled too little."""
    st = polls.stats
    polled = st.polled_s[slot]
    if polled < min_hours * 3600.0:
        return None
    gap = polled / st.hits[slot] if st.hits[slot] else math.inf
    want = round_down(min(factor * gap, NO_NEWS_S))
    limit = page_fill * max_items
    if max_items <= 0 or not st.hits[slot] or polls.busiest(slot, want) <= limit:
        return want
    grid = [s for s in GRID_S if s < want]  # busiest() only grows with the window: the largest that fits
    low, high, best = 0, len(grid) - 1, GRID_S[0]
    while low <= high:
        mid = (low + high) // 2
        if polls.busiest(slot, grid[mid]) <= limit:
            best, low = grid[mid], mid + 1
        else:
            high = mid - 1
    return best


# --------------------------------------------------------------------------- the plan


@dataclass(frozen=True)
class FeedPlan:
    source: str
    feed: str
    max_items: int
    wanted_s: tuple[float | None, ...]  # per slot
    polled_h: tuple[float, ...]
    polls: tuple[int, ...]
    hits: tuple[int, ...]
    max_new: tuple[int, ...]

    def to_record(self) -> dict[str, Any]:
        return {"source": self.source, "feed": self.feed, "max_items": self.max_items,
                "wanted_s": list(self.wanted_s), "polled_h": list(self.polled_h), "polls": list(self.polls),
                "hits": list(self.hits), "max_new": list(self.max_new)}

    @classmethod
    def from_record(cls, d: Mapping[str, Any]) -> FeedPlan:
        def row(key: str, kind: Any, none_ok: bool = False) -> tuple[Any, ...]:
            values = d[key]
            if not isinstance(values, list) or len(values) != SLOTS:
                raise ValueError(f"pace: {key} must list {SLOTS} values")
            for v in values:
                if not ((none_ok and v is None) or (isinstance(v, kind) and not isinstance(v, bool))):
                    raise ValueError(f"pace: bad value in {key}")
            return tuple(float(v) if kind is not int and v is not None else v for v in values)

        source, feed, max_items = d["source"], d["feed"], d["max_items"]
        if not isinstance(source, str) or not isinstance(feed, str) or not isinstance(max_items, int):
            raise ValueError("pace: bad feed")
        return cls(source=source, feed=feed, max_items=max_items, wanted_s=row("wanted_s", (int, float), True),
                   polled_h=row("polled_h", (int, float)), polls=row("polls", int), hits=row("hits", int),
                   max_new=row("max_new", int))


@dataclass(frozen=True)
class PacePlan:
    made_at: datetime
    lookback_from: datetime
    inputs: Mapping[str, Any]  # PaceConfig.plan_inputs() at the time
    feeds: Mapping[tuple[str, str], FeedPlan]

    def to_record(self) -> dict[str, Any]:
        return {"format": FORMAT, "first_seen_at": iso(self.made_at), "lookback_from": iso(self.lookback_from),
                **dict(self.inputs), "feeds": [f.to_record() for f in self.feeds.values()]}

    @classmethod
    def from_record(cls, d: Mapping[str, Any]) -> PacePlan:
        if not isinstance(d, Mapping) or d.get("format") != FORMAT:
            raise ValueError("pace: unknown format")
        made, since = parse_iso(d.get("first_seen_at")), parse_iso(d.get("lookback_from"))
        feeds = d.get("feeds")
        if made is None or since is None or not isinstance(feeds, list):
            raise ValueError("pace: bad record")
        plans = [FeedPlan.from_record(f) for f in feeds]
        inputs = {k: d.get(k) for k in ("factor", "lookback_days", "min_hours", "page_fill")}
        return cls(made_at=made, lookback_from=since, inputs=inputs, feeds={(p.source, p.feed): p for p in plans})


def build_plan(records: Iterable[Mapping[str, Any]], cfg: ScoutConfig, now: datetime) -> PacePlan:
    """The plan for every paced source, from the health records of the lookback ending `now`."""
    p = cfg.pace
    start = now - timedelta(days=p.lookback_days)
    sources = [s for s in cfg.sources.values() if s.enabled and p.paced(s.name)]
    measured = measure(records, start, now, (s.name for s in sources))
    feeds: dict[tuple[str, str], FeedPlan] = {}
    for src in sources:
        for feed in feeds_of(src, cfg):
            polls = measured.get((src.name, feed), FeedPolls.empty())
            st = polls.stats
            feeds[(src.name, feed)] = FeedPlan(
                source=src.name, feed=feed, max_items=src.max_items,
                wanted_s=tuple(wanted_spacing(polls, s, src.max_items, p.factor, p.page_fill, p.min_hours)
                               for s in range(SLOTS)),
                polled_h=tuple(round(v / 3600.0, 2) for v in st.polled_s), polls=tuple(st.polls),
                hits=tuple(st.hits), max_new=tuple(st.max_new))
    return PacePlan(made_at=now, lookback_from=start, inputs=p.plan_inputs(), feeds=feeds)


def _health_between(paths: ScoutPaths, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    day = et_date(start)
    while day <= et_date(end):
        rows.extend(read_jsonl(paths.day_file(paths.health, day)))
        day += timedelta(days=1)
    return rows


def load_latest(paths: ScoutPaths, as_of: datetime | None = None) -> PacePlan | None:
    """The newest readable plan (made by `as_of` when given); None when there is none. Never raises."""
    try:
        files = sorted((f for f in paths.pace.glob("*.jsonl") if _is_day(f.stem)), key=lambda f: f.stem, reverse=True)
    except OSError:
        return None
    for path in files[:MAX_FILES]:
        if as_of is not None and date.fromisoformat(path.stem) > et_date(as_of):
            continue
        for rec in reversed(read_jsonl(path)):
            try:
                plan = PacePlan.from_record(rec)
            except (KeyError, TypeError, ValueError):
                continue
            if as_of is None or plan.made_at <= as_of:
                return plan
    return None


def _is_day(stem: str) -> bool:
    try:
        date.fromisoformat(stem)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- run time


class Pace:
    """The plan in force. `spacing` is asked by the wire and EDGAR schedules, the coverage tracker and
    the heartbeat; `recalc` makes and saves a new plan."""

    def __init__(self, cfg: ScoutConfig, paths: ScoutPaths, log: logging.Logger | None = None) -> None:
        self.cfg, self.paths = cfg, paths
        self.log = log or logging.getLogger("scout.pace")
        self.plan: PacePlan | None = load_latest(paths) if cfg.pace.enabled else None
        self._at: tuple[datetime, int, bool] | None = None  # (now, slot, active): asked many times a tick

    def _where(self, now: datetime) -> tuple[int, bool]:
        at = self._at  # one read: the heartbeat's Pulse thread asks too
        if at is None or at[0] != now:
            at = (now, slot_of(now), in_active_hours(now, self.cfg))
            self._at = at
        return at[1], at[2]

    def spacing(self, src: SourceConfig, feed: str, now: datetime, fixed: float) -> float:
        """The spacing in force for one feed at `now`; `fixed` when it is not paced."""
        plan = self.plan  # one read: `recalc` replaces it whole
        if plan is None or not self.cfg.pace.enabled:
            return float(fixed)
        feed_plan = plan.feeds.get((src.name, feed))
        if feed_plan is None:
            return float(fixed)
        slot, active = self._where(now)
        return clamp(feed_plan.wanted_s[slot], fixed, self.cfg.pace.slowest(src.name, feed, active))

    def source_spacing(self, src: SourceConfig, now: datetime, fixed: float) -> float:
        """The fastest of the source's feeds: how often the source is polled at all."""
        return min((self.spacing(src, f, now, fixed) for f in feeds_of(src, self.cfg)), default=float(fixed))

    def stale(self, now: datetime) -> bool:
        plan = self.plan
        if plan is None or dict(plan.inputs) != self.cfg.pace.plan_inputs():
            return True
        age = (now - plan.made_at).total_seconds()
        return age < 0 or age > STALE_AFTER_S

    def recalc(self, now: datetime) -> PacePlan:
        start = now - timedelta(days=self.cfg.pace.lookback_days)
        plan = build_plan(_health_between(self.paths, start, now), self.cfg, now)
        append_jsonl(self.paths, self.paths.day_file(self.paths.pace, et_date(now)), plan.to_record())
        self.plan = plan
        slowed = sum(1 for f in plan.feeds.values() if any(w is not None for w in f.wanted_s))
        self.log.info("pace: recalculated from %s; %d of %d feeds measured", iso(start), slowed, len(plan.feeds))
        return plan


def requests_per_day(spacings: Sequence[float], hours: float = 1.0) -> float:
    """Requests in a day of slots each `hours` long at these spacings."""
    return sum(hours * 3600.0 / s for s in spacings if s > 0)
