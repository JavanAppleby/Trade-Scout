"""Scout 0.2.4: the polling pace (owner, 2026-11-10: "Slow Scout down by a factor based on how frequent
each source has updated data - and continue to monitor and adjust timings as time goes on"; DESIGN 21).
No network: a fake transport over a ManualClock."""

from __future__ import annotations

import json
import logging
import tomllib
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from helpers_fakes import FakeResponse
from helpers_wire import full_transport, wire_config
from scout_reader.coverage import gaps
from scout_reader.records import read_jsonl

from scout import jobs_pace, wire_rss
from scout.app import AppContext
from scout.brief_pace import hour_ranges, sec_pace
from scout.brief_sections import BriefInputs
from scout.clock import ManualClock, et_date, iso, to_et
from scout.config import ScoutConfig, config_from_dict
from scout.config_load import ConfigError
from scout.health import CoverageTracker, recorded_spacing, spacing_in_force
from scout.heartbeat import SourceStatusBoard
from scout.http import HttpFetcher
from scout.jobs_wire import build_jobs
from scout.models import SourceState
from scout.pace import (
    CLOSED, NO_NEWS_S, SLOTS, WIRE_FEED, FeedPlan, FeedPolls, Pace, PacePlan, load_latest, measure, round_down,
    slot_of, wanted_spacing,
)
from scout.paths import ScoutPaths
from scout.scheduler import run_due
from scout.store import append_jsonl

from conftest import et_at

LOG = logging.getLogger("scout.test")
MON, TUE, SAT = "2026-11-02", "2026-11-03", "2026-11-07"
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"
EMAIL = {"contact_email": "test@example.invalid"}


def _cfg(scout_dir: Path, **pace: object) -> ScoutConfig:
    return config_from_dict({**EMAIL, **({"pace": pace} if pace else {})}, scout_dir)


def plan_with(cfg: ScoutConfig, feeds: dict, made_at: datetime) -> PacePlan:
    """A plan whose wanted spacing per slot is given ({(source, feed): {slot: seconds}})."""
    out = {}
    for (source, feed), slots in feeds.items():
        out[(source, feed)] = FeedPlan(source, feed, cfg.source(source).max_items,
                                       tuple(slots.get(i) for i in range(SLOTS)), (1.0,) * SLOTS, (10,) * SLOTS,
                                       (1,) * SLOTS, (0,) * SLOTS)
    return PacePlan(made_at, made_at - timedelta(days=7), cfg.pace.plan_inputs(), out)


def hrec(source: str, feed: str, at: datetime, *, new: int = 0, result: str = "ok", spacing: float = 60.0,
         items: int = 20) -> dict:
    return {"format": 1, "source": source, "feed": feed, "kind": "wire", "enabled": True, "gate6": True,
            "start": iso(at), "end": iso(at + timedelta(seconds=1)), "result": result, "http_status": 200,
            "items": items, "new_items": new, "overlap": True, "spacing_s": spacing, "gap_before": False,
            "error": None, "seen_items": items - new}


class Timed:
    """The wire unit's fixture transport over a ManualClock: each request takes 0.2 s; records when
    each URL was asked."""

    def __init__(self, cfg: ScoutConfig, clock: ManualClock) -> None:
        self.inner, self.clock = full_transport(cfg), clock
        self.calls: list[tuple[datetime, str]] = []

    def __call__(self, url: str, headers, timeout_s: float, max_bytes: int) -> FakeResponse:
        self.calls.append((self.clock.now(), url))
        self.clock.advance(0.2)
        return self.inner(url, headers, timeout_s, max_bytes)

    def times(self, fragment: str) -> list[datetime]:
        return [at for at, url in self.calls if fragment in url]


def _ctx(cfg: ScoutConfig, clock: ManualClock) -> tuple[AppContext, Timed]:
    ctx = AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, board=SourceStatusBoard(cfg), log=LOG)
    ctx.paths.ensure()
    transport = Timed(cfg, clock)
    ctx._services["fetcher"] = HttpFetcher(cfg, clock, transport=transport)
    return ctx, transport


def _loop(jobs: list, clock: ManualClock, until: datetime) -> None:
    while clock.now() < until:  # the main loop (scout.app.run)
        run_due(jobs, clock.now(), LOG, clock)
        clock.sleep(1.0)


def _intervals(times: list[datetime]) -> list[float]:
    return [(b - a).total_seconds() for a, b in zip(times, times[1:])]


# --------------------------------------------------------------------------- measuring and the rule


def test_slots_are_et_hours_of_trading_days_and_one_for_closed_days() -> None:
    assert slot_of(et_at(MON, "00:30")) == 0 and slot_of(et_at(MON, "09:59")) == 9
    assert slot_of(et_at(MON, "23:59")) == 23
    assert slot_of(et_at(SAT, "10:00")) == CLOSED and slot_of(et_at("2026-11-26", "10:00")) == CLOSED  # Thanksgiving
    assert WIRE_FEED == wire_rss.FEED


def test_round_down() -> None:
    assert [round_down(s) for s in (0.4, 5.5, 65, 149.9, 599, 650, 3599, NO_NEWS_S)] == \
        [1, 5, 60, 140, 590, 600, 3540, NO_NEWS_S]


def test_measure_counts_polled_time_hits_and_the_fastest_burst() -> None:
    t = et_at(MON, "10:00")
    rows = [hrec("prnewswire", "rss", t + timedelta(seconds=60 * i), new=n) for i, n in enumerate([0, 2, 0, 0, 10, 0])]
    rows.append(hrec("prnewswire", "rss", t + timedelta(seconds=400), result="timeout"))  # failed: not counted
    rows.append(hrec("stocktitan", "rss", t, new=5))  # another source
    rows.append({**hrec("prnewswire", "rss", t + timedelta(seconds=420), new=7), "format": 2})  # unknown format
    rows.append(hrec("prnewswire", "rss", t - timedelta(days=8), new=20))  # before the lookback
    rows.append(hrec("prnewswire", "rss", t + timedelta(hours=2), new=20))  # not finished by the end (no look-ahead)
    polls = measure(rows, t - timedelta(days=7), t + timedelta(hours=1), ["prnewswire"])[("prnewswire", "rss")]
    st = polls.stats
    assert st.polls[10] == 6 and st.polled_s[10] == 360 and st.hits[10] == 2 and st.max_new[10] == 10
    assert sum(st.polls) == 6
    assert polls.busiest(10, 60) == 10  # one poll's worth
    assert polls.busiest(10, 240) == 12  # the 2 and the 10 fall within four minutes


def _slot10(news: list[int]) -> FeedPolls:
    """60 s PR Newswire polls in the 10:00 ET hour of Mon, Tue and Wed (60 a day), these new-item counts."""
    days = [et_at(d, "10:00") for d in (MON, TUE, "2026-11-04")]
    rows = [hrec("prnewswire", "rss", days[i // 60] + timedelta(seconds=60 * (i % 60)), new=n)
            for i, n in enumerate(news)]
    return measure(rows, days[0], days[-1] + timedelta(hours=1), ["prnewswire"])[("prnewswire", "rss")]


def test_wanted_spacing_is_a_factor_of_the_gap() -> None:
    quiet = _slot10([1 if i % 10 == 0 else 0 for i in range(180)])  # 3 h, something new every 10 minutes
    assert wanted_spacing(quiet, 10, 20, 0.5, 0.5, 2.0) == 300  # 0.5 x 600 s
    assert wanted_spacing(quiet, 10, 20, 0.25, 0.5, 2.0) == 150
    assert wanted_spacing(quiet, 10, 20, 0.5, 0.5, 4.0) is None  # polled 3 h < min_hours: not measured
    assert wanted_spacing(_slot10([0] * 180), 10, 20, 0.5, 0.5, 2.0) == NO_NEWS_S  # nothing new: the slowest applies


def test_the_page_guard_keeps_a_bursty_hour_fast() -> None:
    burst = [1 if i % 10 == 0 else 0 for i in range(180)]
    burst[95] = 20  # once, 20 releases within a minute
    assert wanted_spacing(_slot10(burst), 10, 20, 0.5, 0.5, 2.0) == 10  # no spacing keeps that burst within half a page
    assert wanted_spacing(_slot10(burst), 10, 100, 0.5, 0.5, 2.0) == 280  # a 100-item page holds it: 0.5 x 10800 / 19
    sustained = [0] * 180
    for i in range(60, 70):
        sustained[i] = 3  # ten minutes of 3 releases a minute
    # the factor alone would give 540 s (0.5 x 10800 / 10); half of a 20-item page holds 3 polls of 3
    assert wanted_spacing(_slot10(sustained), 10, 20, 0.5, 0.5, 2.0) == 180
    assert wanted_spacing(_slot10(sustained), 10, 0, 0.5, 0.5, 2.0) == 540  # no page size: no guard


# --------------------------------------------------------------------------- the spacing in force


def test_the_pace_stays_between_the_fixed_spacing_and_the_slowest_allowed(scout_dir: Path) -> None:
    cfg = _cfg(scout_dir)
    pace = Pace(cfg, ScoutPaths.from_config(cfg))
    pace.plan = plan_with(cfg, {("edgar_atom", "8-K"): {10: 30.0, 11: 240.0, 12: 9999.0, 2: 9999.0, CLOSED: 9999.0},
                                ("edgar_atom", "10-K"): {10: 9999.0}, ("prnewswire", "rss"): {10: 9999.0}},
                          et_at(MON, "03:30"))
    src, prn = cfg.source("edgar_atom"), cfg.source("prnewswire")
    assert pace.spacing(src, "8-K", et_at(MON, "10:30"), 60) == 60  # never faster than the fixed spacing
    assert pace.spacing(src, "8-K", et_at(MON, "11:30"), 60) == 240
    assert pace.spacing(src, "8-K", et_at(MON, "12:30"), 60) == 300  # trading hours: at most 5 minutes
    assert pace.spacing(src, "8-K", et_at(MON, "02:30"), 600) == 1800  # nights: at most 30 minutes
    assert pace.spacing(src, "8-K", et_at(SAT, "10:30"), 600) == 1800  # closed days too
    assert pace.spacing(src, "8-K", et_at(MON, "13:30"), 60) == 60  # an hour not measured: fixed
    assert pace.spacing(src, "10-K", et_at(MON, "10:30"), 60) == 1800  # the form's own line wins
    assert pace.spacing(src, "S-1", et_at(MON, "10:30"), 60) == 60  # a feed not in the plan: fixed
    assert pace.spacing(prn, "rss", et_at(MON, "10:30"), 60) == 120  # a wire: at most 2 minutes
    assert spacing_in_force(prn, et_at(MON, "10:30"), cfg, pace, "rss") == 120
    assert spacing_in_force(prn, et_at(MON, "10:59"), cfg, pace, "rss") == 30  # the hour turn is kept
    assert spacing_in_force(src, et_at(MON, "12:30"), cfg, pace) == 60  # the source: its fastest form
    assert spacing_in_force(src, et_at(MON, "12:30"), cfg) == 60  # no pace: as before 0.2.4


def test_pacing_off_or_a_feed_without_a_line_keeps_the_fixed_spacing(scout_dir: Path) -> None:
    feeds = {("edgar_atom", "8-K"): {12: 9999.0}}
    off = _cfg(scout_dir, enabled=False)
    pace = Pace(off, ScoutPaths.from_config(off))
    pace.plan = plan_with(off, feeds, et_at(MON, "03:30"))
    assert pace.spacing(off.source("edgar_atom"), "8-K", et_at(MON, "12:30"), 60) == 60
    unpaced = _cfg(scout_dir, slowest_active_s={"prnewswire": 120.0})  # the table replaces the default one
    pace = Pace(unpaced, ScoutPaths.from_config(unpaced))
    pace.plan = plan_with(unpaced, feeds, et_at(MON, "03:30"))
    assert pace.spacing(unpaced.source("edgar_atom"), "8-K", et_at(MON, "12:30"), 60) == 60
    zero = _cfg(scout_dir, slowest_active_s={"edgar_atom": 300.0, "edgar_atom:8-K": 0.0})  # 0 = not paced
    pace = Pace(zero, ScoutPaths.from_config(zero))
    pace.plan = plan_with(zero, {**feeds, ("edgar_atom", "6-K"): {12: 9999.0}}, et_at(MON, "03:30"))
    assert pace.spacing(zero.source("edgar_atom"), "8-K", et_at(MON, "12:30"), 60) == 60
    assert pace.spacing(zero.source("edgar_atom"), "6-K", et_at(MON, "12:30"), 60) == 300


# --------------------------------------------------------------------------- the plan, saved


def _history(paths: ScoutPaths, end: datetime, days: int, hit: Callable[[datetime], int]) -> None:
    """60 s PR Newswire polls for `days` days up to `end`; `hit(at)` gives each poll's new items."""
    by_day: dict[str, list[str]] = {}
    at = end - timedelta(days=days)
    while at < end:
        by_day.setdefault(et_date(at).isoformat(), []).append(json.dumps(hrec("prnewswire", "rss", at, new=hit(at))))
        at += timedelta(seconds=60)
    for day, lines in by_day.items():
        (paths.health / f"{day}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_recalc_slows_the_quiet_hours_only_and_survives_a_restart(scout_dir: Path) -> None:
    cfg = _cfg(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    paths.ensure()
    now = et_at("2026-11-07", "00:00")  # Saturday: Mon-Fri of history, no closed-day hour in it

    def hit(at: datetime) -> int:
        local = to_et(at)
        if local.hour == 8:
            return 1  # busy: something new on every poll
        if local.hour == 21 and local.minute == 5:
            return 20  # a burst that fills the 20-item page
        return 1 if local.minute == 0 else 0  # quiet: one new item an hour

    _history(paths, now, 5, hit)
    plan = Pace(cfg, paths).recalc(now)
    again = Pace(cfg, paths)  # a restart reads it back
    assert again.plan is not None and again.plan.feeds == plan.feeds and again.plan.made_at == plan.made_at
    prn = cfg.source("prnewswire")
    assert again.spacing(prn, "rss", et_at("2026-11-09", "08:30"), 60) == 60  # busy hour: as fast as before
    assert again.spacing(prn, "rss", et_at("2026-11-09", "13:30"), 60) == 120  # quiet trading hour: at most 2 min
    assert again.spacing(prn, "rss", et_at("2026-11-09", "02:30"), 60) == 1800  # quiet night: at most 30 min
    assert again.spacing(prn, "rss", et_at("2026-11-09", "21:30"), 60) == 60  # the burst keeps 21:00 fast
    assert plan.feeds[("prnewswire", "rss")].wanted_s[CLOSED] is None  # no weekend history: fixed
    assert not again.stale(now + timedelta(hours=1)) and again.stale(now + timedelta(hours=27))
    assert again.stale(now - timedelta(hours=1))  # a plan "from the future" (clock stepped back)
    assert Pace(_cfg(scout_dir, factor=0.25), paths).stale(now + timedelta(hours=1))  # other settings
    path = paths.pace / "2026-11-07.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"format": 1, "first_seen_at": "2026-11-07T09:00:00Z", "feeds": "damaged"}\n')
    assert Pace(cfg, paths).plan.made_at == plan.made_at  # a damaged line: the one before it
    assert load_latest(paths, as_of=now - timedelta(seconds=1)) is None  # as of: not seen yet
    assert len(read_jsonl(path)) == 2


def test_jobs_recalculate_at_start_and_every_day(scout_dir: Path) -> None:
    cfg = _cfg(scout_dir)
    clock = ManualClock(et_at(MON, "05:00"))
    ctx, _t = _ctx(cfg, clock)
    jobs = jobs_pace.build_jobs(ctx)
    assert [j.name for j in jobs] == ["pace_start", "pace"]
    assert run_due(jobs, clock.now(), LOG, clock) == ["pace_start", "pace"]  # the start steps aside for the daily
    assert len(read_jsonl(ctx.paths.pace / f"{MON}.jsonl")) == 1
    clock.set(et_at(MON, "09:00"))
    assert run_due(jobs, clock.now(), LOG, clock) == []  # done for today
    clock.set(et_at(TUE, "03:30"))
    assert run_due(jobs, clock.now(), LOG, clock) == ["pace"]
    clock.set(et_at(TUE, "04:00"))  # a restart after today's recalculation: no second read
    ctx2, _t2 = _ctx(cfg, clock)
    jobs2 = jobs_pace.build_jobs(ctx2)
    assert run_due(jobs2, clock.now(), LOG, clock) == ["pace_start"]
    assert len(read_jsonl(ctx.paths.pace / f"{TUE}.jsonl")) == 1
    assert jobs_pace.build_jobs(_ctx(_cfg(scout_dir / "off", enabled=False), clock)[0]) == []


# --------------------------------------------------------------------------- the schedules


def test_each_edgar_form_is_polled_at_its_own_pace(scout_dir: Path) -> None:
    cfg = wire_config(scout_dir, edgar_atom_forms=["8-K", "F-1"])
    start = et_at(MON, "10:00")
    clock = ManualClock(start)
    ctx, transport = _ctx(cfg, clock)
    ctx.pace().plan = plan_with(cfg, {("edgar_atom", "8-K"): {10: 60.0}, ("edgar_atom", "F-1"): {10: 9999.0}}, start)
    _loop(build_jobs(ctx), clock, start + timedelta(minutes=20))
    eight_k, f1 = transport.times("type=8-K"), transport.times("type=F-1")
    assert 19 <= len(eight_k) <= 21 and max(_intervals(eight_k)) <= 61
    assert len(f1) == 4 and all(300 <= s <= 310 for s in _intervals(f1))  # the 5-minute limit in trading hours
    health = [r for r in read_jsonl(ctx.paths.health / f"{MON}.jsonl") if r["source"] == "edgar_atom"]
    assert {r["spacing_s"] for r in health if r["feed"] == "F-1"} == {300.0}
    assert {r["spacing_s"] for r in health if r["feed"] == "8-K"} == {60.0}
    assert gaps(health, "edgar_atom", start + timedelta(minutes=2), clock.now(), max_gap_s=180,
                feeds=["8-K", "F-1"], overlap_max_gap_s=1800) == []
    assert ctx.board.sources["edgar_atom"].status == "ok"
    assert not ctx.board.states_at(clock.now())["edgar_atom"].gap_open


def test_a_switch_to_a_slower_pace_opens_no_false_gap(scout_dir: Path) -> None:
    """09:00-10:00 at 60 s, 10:00 on at 300 s: the last poll before 10:00 records the 300 s it may wait."""
    cfg = wire_config(scout_dir, edgar_atom_forms=["8-K"])
    start = et_at(MON, "09:50")
    clock = ManualClock(start)
    ctx, transport = _ctx(cfg, clock)
    ctx.pace().plan = plan_with(cfg, {("edgar_atom", "8-K"): {9: 60.0, 10: 9999.0}}, start)
    jobs = build_jobs(ctx)
    tracker_seen: list[bool] = []
    end = et_at(MON, "10:20")
    tracker = next(j for j in jobs if j.name == "edgar_atom").fn.__self__.tracker
    while clock.now() < end:
        run_due(jobs, clock.now(), LOG, clock)
        tracker_seen.append(tracker.gap_open("edgar_atom", clock.now()))
        assert not ctx.board.states_at(clock.now())["edgar_atom"].gap_open, iso(clock.now())
        clock.sleep(1.0)
    health = [r for r in read_jsonl(ctx.paths.health / f"{MON}.jsonl") if r["feed"] == "8-K"]
    last_before = [r for r in health if r["start"] < iso(et_at(MON, "10:00"))][-1]
    assert last_before["spacing_s"] == 300.0  # the wait to its next poll
    assert max(_intervals(transport.times("type=8-K"))) <= 301
    assert not any(tracker_seen[5:])
    for minutes in range(9, 30):  # as of any moment in the slower hour: no gap
        as_of = start + timedelta(minutes=minutes)
        assert gaps(health, "edgar_atom", start + timedelta(minutes=2), as_of, max_gap_s=180, feeds=["8-K"],
                    overlap_max_gap_s=1800) == [], iso(as_of)


def test_the_poll_before_the_end_of_active_hours_records_the_idle_spacing(scout_dir: Path) -> None:
    """Also without pacing (0.2.4): at 20:00 ET EDGAR moves from 60 s to 600 s."""
    cfg = _cfg(scout_dir)
    src = cfg.source("edgar_atom")
    assert recorded_spacing(src, et_at(MON, "19:59"), cfg) == 600
    assert recorded_spacing(src, et_at(MON, "19:58"), cfg) == 60
    assert recorded_spacing(src, et_at(MON, "03:55"), cfg) == 600  # the faster day spacing only brings it earlier
    tracker = CoverageTracker(cfg)
    assert tracker.pace is None


def test_wires_follow_the_pace_and_keep_the_hour_turn(scout_dir: Path) -> None:
    cfg = wire_config(scout_dir, sources={"edgar_atom": {"enabled": False}})
    start = et_at(MON, "10:10")
    clock = ManualClock(start)
    ctx, transport = _ctx(cfg, clock)
    ctx.pace().plan = plan_with(cfg, {("prnewswire", "rss"): {10: 9999.0, 11: 9999.0},
                                      ("stocktitan", "rss"): {10: 90.0, 11: 90.0}}, start)
    _loop(build_jobs(ctx), clock, et_at(MON, "11:10"))
    prn = transport.times("prnewswire")
    quiet = [t for t in prn if t < et_at(MON, "10:57")]
    assert all(119 <= s <= 121 for s in _intervals(quiet))
    turn = [t for t in prn if et_at(MON, "10:58") <= t < et_at(MON, "11:05")]
    assert 12 <= len(turn) <= 15  # every 30 s from :58 to :05, as before
    assert all(89 <= s <= 91 for s in _intervals(transport.times("stocktitan")))


def test_the_halt_feed_is_never_paced(scout_dir: Path) -> None:
    with pytest.raises(ConfigError, match="only wire, filing"):
        _cfg(scout_dir, slowest_active_s={"nasdaq_halts": 300.0})
    cfg = _cfg(scout_dir)
    assert cfg.pace.slowest("nasdaq_halts", "tradehalts", True) is None and not cfg.pace.paced("nasdaq_halts")


def test_the_heartbeat_ages_a_paced_source_by_its_pace(scout_dir: Path) -> None:
    cfg = _cfg(scout_dir)
    board = SourceStatusBoard(cfg)
    last = et_at(MON, "12:00")
    board.update("stocktitan", SourceState(enabled=True, status="ok", last_ok=iso(last), last_error=None,
                                           gap_open=False))
    at = last + timedelta(seconds=200)
    assert board.states_at(at)["stocktitan"].gap_open  # fixed 60 s: older than 180 s is a gap
    pace = Pace(cfg, ScoutPaths.from_config(cfg))
    pace.plan = plan_with(cfg, {("stocktitan", "rss"): {12: 9999.0}}, last)
    board.pace = pace
    assert not board.states_at(at)["stocktitan"].gap_open  # paced at 120 s: 2 x 120 allowed
    assert board.states_at(last + timedelta(seconds=241))["stocktitan"].gap_open


# --------------------------------------------------------------------------- config and the brief


@pytest.mark.parametrize("pace, needle", [
    ({"factor": 0}, "pace.factor"), ({"factor": 1.5}, "pace.factor"), ({"lookback_days": 1}, "pace.lookback_days"),
    ({"min_hours": 0}, "pace.min_hours"), ({"page_fill": 0}, "pace.page_fill"),
    ({"slowest_active_s": {"prnewswire": 10}}, "pace.slowest_active_s.prnewswire"),
    ({"slowest_idle_s": {"prnewswire": 7200}}, "pace.slowest_idle_s.prnewswire"),
    ({"slowest_active_s": {"nope": 300}}, "no source named"),
    ({"slowest_active_s": {"edgar_atom:8K": 300}}, "not one of edgar_atom_forms"),
    ({"slowest_active_s": {"prnewswire:rss": 300}}, "only an EDGAR source"),
    ({"bogus": 1}, "pace.bogus"), ({"recalc_time_et": "3pm"}, "pace.recalc_time_et"),
    ({"enabled": "yes"}, "pace.enabled"), ({"slowest_idle_s": 5}, "pace.slowest_idle_s"),
])
def test_bad_pace_settings_are_refused(scout_dir: Path, pace: dict, needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        config_from_dict({"pace": pace}, scout_dir)


def test_pace_defaults_match_the_example(scout_dir: Path) -> None:
    c = config_from_dict({}, scout_dir)
    p = c.pace
    assert p.enabled and p.factor == 0.5 and p.lookback_days == 7 and p.page_fill == 0.5 and p.min_hours == 2.0
    assert p.slowest("edgar_atom", "8-K", True) == 300 and p.slowest("edgar_atom", "10-K", True) == 1800
    assert p.slowest("edgar_atom", "8-K", False) == 1800 and p.slowest("prnewswire", "rss", True) == 120
    assert config_from_dict(tomllib.loads(EXAMPLE.read_text(encoding="utf-8")), scout_dir).pace == p
    # a default line for a form this install does not poll is simply unused
    assert config_from_dict({"edgar_atom_forms": ["8-K"]}, scout_dir).pace.slowest("edgar_atom", "8-K", True) == 300


def test_hour_ranges() -> None:
    values = [1800.0] * 4 + [60.0] * 6 + [300.0] * 10 + [1800.0] * 4
    assert hour_ranges(values) == "60 s 04:00-10:00, 5 min 10:00-20:00, 30 min 20:00-04:00"
    assert hour_ranges([60.0] * 24) == "60 s all day"
    assert hour_ranges([90.0] * 23 + [150.0]) == "90 s 04:00-23:00, 2.5 min 23:00-00:00, 90 s 00:00-04:00"
    assert hour_ranges([1800.0] * 16 + [570.0] + [1800.0] * 7) == \
        "30 min 04:00-16:00, 9.5 min 16:00-17:00, 30 min 17:00-04:00"  # from the start of the trading day


def test_the_brief_shows_each_feeds_pace(scout_dir: Path) -> None:
    cfg = _cfg(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    paths.ensure()
    made = et_at(MON, "03:30")
    inp = BriefInputs(cfg=cfg, paths=paths, day=made.date(), as_of=et_at(MON, "08:20"), llm={}, finds=[],
                      radar_skipped=True)
    assert "No pace worked out yet" in "\n".join(sec_pace(inp))
    wanted = {**{h: 30.0 for h in range(4, 10)}, **{h: 9999.0 for h in [*range(10, 24), *range(0, 4), CLOSED]}}
    append_jsonl(paths, paths.day_file(paths.pace, made.date()),
                 plan_with(cfg, {("edgar_atom", "8-K"): wanted}, made).to_record())
    text = "\n".join(sec_pace(inp))
    assert ("- edgar\\_atom 8-K: 60 s 04:00-10:00, 5 min 10:00-20:00, 30 min 20:00-04:00; closed 30 min "
            "(fixed: 60 s in trading hours, 10 min otherwise); new items in 10% of 250 polls.") in text
    assert "About 496 requests a trading day for these feeds (1,008 at the fixed spacing)." in text
    assert "Not paced: nasdaq_halts" in text
    early = BriefInputs(cfg=cfg, paths=paths, day=made.date(), as_of=made - timedelta(minutes=1), llm={}, finds=[],
                        radar_skipped=True)
    assert "No pace worked out yet" in "\n".join(sec_pace(early))  # as of: the plan was not made yet
    off = _cfg(scout_dir, enabled=False)
    assert "Pacing is off" in "\n".join(sec_pace(BriefInputs(cfg=off, paths=paths, day=made.date(), as_of=made,
                                                               llm={}, finds=[], radar_skipped=True)))
