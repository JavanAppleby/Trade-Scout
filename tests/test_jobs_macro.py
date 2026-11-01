"""The macro unit's jobs (DESIGN 2.3, 8)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from conftest import make_config
from helpers_fakes import FakeFetcher
from helpers_macro import (BLS_URL, CAL_URL, HALTS_URL, VIX_URL, FakeCtx, halts_body, macro_body,
                           nowflation_results)

from scout.jobs_macro import build_jobs
from scout.paths import ScoutPaths
from scout.scheduler import run_due

LOG = logging.getLogger("scout.test.jobs_macro")


def _results() -> dict:
    return {HALTS_URL: halts_body(), BLS_URL: macro_body("bls_SYNTHETIC.ics"),
            VIX_URL: macro_body("VIX_History_head_SYNTHETIC.csv"), **nowflation_results()}


def _ctx(cfg, clock) -> FakeCtx:
    return FakeCtx(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, fetch=FakeFetcher(_results()))


def _history(ctx) -> list[dict]:
    path = ctx.paths.macro / "history.jsonl"
    return [json.loads(x) for x in path.read_text("utf-8").splitlines()] if path.exists() else []


def test_job_names(cfg, clock) -> None:
    names = [j.name for j in build_jobs(_ctx(cfg, clock))]
    assert names == ["nasdaq_halts", "fomc_start", "fomc", "bls", "nowflation_start", "nowflation@08:20",
                     "nowflation_release_day", "cboe_vix_start", "cboe_vix"]


def test_disabled_sources_get_no_job(scout_dir, clock) -> None:
    off = {name: {"enabled": False} for name in ("nasdaq_halts", "nowflation", "bls", "cboe_vix")}
    cfg = make_config(scout_dir, sources=off)
    assert [j.name for j in build_jobs(_ctx(cfg, clock))] == ["fomc_start", "fomc"]


def test_first_tick_reads_every_source_once(cfg, clock, et) -> None:
    now = et("2026-11-02", "09:00")  # after 08:20: the daily read and the start read would both be due
    clock.set(now)
    ctx = _ctx(cfg, clock)
    jobs = build_jobs(ctx)
    ran = run_due(jobs, now, LOG)
    assert "nowflation@08:20" in ran and "nowflation_start" in ran
    urls = [u for _s, u in ctx.fetch.calls]
    assert urls.count(CAL_URL) == 1  # the start read stepped aside
    assert urls.count(HALTS_URL) == 1 and urls.count(BLS_URL) == 1
    assert urls.count(VIX_URL) == 1  # before 16:30 the start read runs; the daily one is not due
    sources = [h["source"] for h in _history(ctx)]
    assert sources.count("nowflation") == 1 and set(sources) == {"fomc", "bls", "nowflation", "cboe_vix"}
    daily = json.loads((ctx.paths.macro / "2026-11-02.json").read_text("utf-8"))
    assert daily["inflation_regime"] == "steady"
    assert {f.kind for f in ctx.fact_sink.facts} == {"halt", "macro_event"}
    assert ctx.board.sources["nowflation"].status == "ok"
    # the next minute: only the halt poll
    clock.advance(60)
    assert run_due(jobs, clock.now(), LOG) == ["nasdaq_halts"]


def test_release_day_read(cfg, clock, et) -> None:
    start = et("2026-11-06", "08:30")  # Friday, NFP at 08:30 ET
    clock.set(start)
    ctx = _ctx(cfg, clock)
    jobs = build_jobs(ctx)
    run_due(jobs, start, LOG)
    assert [u for _s, u in ctx.fetch.calls].count(CAL_URL) == 1
    clock.set(et("2026-11-06", "08:50"))
    ran = run_due(jobs, clock.now(), LOG)
    assert "nowflation_release_day" in ran
    assert [u for _s, u in ctx.fetch.calls].count(CAL_URL) == 2


def test_release_day_job_skips_a_read_already_made_this_tick(cfg, clock, et) -> None:
    now = et("2026-11-06", "09:00")
    clock.set(now)
    ctx = _ctx(cfg, clock)
    jobs = build_jobs(ctx)
    run_due(jobs, now, LOG)  # start skipped, 08:20 read, release-day job sees that read
    assert [u for _s, u in ctx.fetch.calls].count(CAL_URL) == 1


def test_not_a_release_day(cfg, clock, et) -> None:
    clock.set(et("2026-11-02", "08:30"))
    ctx = _ctx(cfg, clock)
    jobs = build_jobs(ctx)
    run_due(jobs, clock.now(), LOG)
    clock.set(et("2026-11-02", "08:50"))
    run_due(jobs, clock.now(), LOG)
    assert [u for _s, u in ctx.fetch.calls].count(CAL_URL) == 1


def test_vix_daily_after_the_close(cfg, clock, et) -> None:
    clock.set(et("2026-11-02", "16:45"))
    ctx = _ctx(cfg, clock)
    jobs = build_jobs(ctx)
    run_due(jobs, clock.now(), LOG)
    assert [u for _s, u in ctx.fetch.calls].count(VIX_URL) == 1
    clock.advance(3600)
    run_due(jobs, clock.now(), LOG)
    assert [u for _s, u in ctx.fetch.calls].count(VIX_URL) == 1


def test_bls_weekly(cfg, clock, et) -> None:
    ctx = _ctx(cfg, clock)
    jobs = [j for j in build_jobs(ctx) if j.name == "bls"]
    now = et("2026-11-02", "09:00")
    run_due(jobs, now, LOG)
    run_due(jobs, now + timedelta(days=6), LOG)
    run_due(jobs, now + timedelta(days=7), LOG)
    assert [u for _s, u in ctx.fetch.calls].count(BLS_URL) == 2


def test_a_failing_source_never_stops_the_others(cfg, clock, et) -> None:
    clock.set(et("2026-11-02", "09:00"))
    ctx = _ctx(cfg, clock)
    ctx.fetch.results.pop(BLS_URL)  # a network failure
    ctx.fetch.results[VIX_URL] = b"garbage"
    run_due(build_jobs(ctx), clock.now(), LOG)
    by = {h["source"]: h for h in _history(ctx)}
    assert by["bls"]["status"] == "unavailable" and by["cboe_vix"]["status"] == "unavailable"
    assert by["nowflation"]["status"] == "ok"


def test_a_restart_does_not_fetch_bls_cboe_or_nowflation_again(cfg, clock, et) -> None:
    """Regression: every start read nowflation (3 requests), BLS and Cboe again; restarts on a CPI
    day went over nowflation's 10 a day, and BLS / Cboe were fetched more than weekly / daily. The
    last read now comes from the macro history, which a restart reloads."""
    now = et("2026-11-02", "07:00")
    clock.set(now)
    ctx = _ctx(cfg, clock)
    run_due(build_jobs(ctx), now, LOG)
    first = [u for _s, u in ctx.fetch.calls]
    assert first.count(BLS_URL) == 1 and first.count(VIX_URL) == 1 and first.count(CAL_URL) == 1
    for minutes in (10, 20):  # two restarts the same morning: new jobs, same Scout folder
        later = now + timedelta(minutes=minutes)
        clock.set(later)
        again = _ctx(cfg, clock)
        run_due(build_jobs(again), later, LOG)
        urls = [u for _s, u in again.fetch.calls]
        assert urls.count(BLS_URL) == 0 and urls.count(VIX_URL) == 0 and urls.count(CAL_URL) == 0
    # the next morning: nowflation's start read runs again (a new ET day); Cboe too (a new close)
    nxt = et("2026-11-03", "07:00")
    clock.set(nxt)
    ctx3 = _ctx(cfg, clock)
    run_due(build_jobs(ctx3), nxt, LOG)
    urls = [u for _s, u in ctx3.fetch.calls]
    assert urls.count(CAL_URL) == 1 and urls.count(VIX_URL) == 1 and urls.count(BLS_URL) == 0


def test_bls_after_a_restart_waits_out_the_week(cfg, clock, et) -> None:
    now = et("2026-11-02", "09:00")
    ctx = _ctx(cfg, clock)
    run_due([j for j in build_jobs(ctx) if j.name == "bls"], now, LOG)
    ctx2 = _ctx(cfg, clock)
    jobs = [j for j in build_jobs(ctx2) if j.name == "bls"]
    for hours in (1, 24, 6 * 24):
        run_due(jobs, now + timedelta(hours=hours), LOG)
    assert ctx2.fetch.calls == []
    run_due(jobs, now + timedelta(days=7, hours=1), LOG)
    assert [u for _s, u in ctx2.fetch.calls] == [BLS_URL]


def test_halts_are_polled_every_minute_when_earlier_jobs_delay_the_send(cfg, clock, et) -> None:
    """Regression: the halts job ran every 60 s from the tick while HaltPoller.ready timed its 60 s
    from the real send (a few seconds into the tick, after the wire jobs), so every other due tick
    found ready() short and the feed was read every 120 s."""
    from scout.scheduler import EveryJob

    class SendTimeFetcher(FakeFetcher):
        sent = None

        def fetch(self, source, url, **kw):
            self.sent = clock.now()
            return super().fetch(source, url, **kw)

        def last_request_at(self, host):
            return self.sent

    start = et("2026-11-02", "10:00")
    clock.set(start)
    ctx = FakeCtx(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, fetch=SendTimeFetcher(_results()))
    halts = [j for j in build_jobs(ctx) if j.name == "nasdaq_halts"]
    wire = EveryJob("wire", lambda now: clock.advance(3), interval_s=60)  # runs first and takes 3 s
    for second in range(600):
        tick = start + timedelta(seconds=second)
        if clock.now() < tick:
            clock.set(tick)
        run_due([wire, *halts], clock.now(), LOG)
    sends = [u for _s, u in ctx.fetch.calls].count(HALTS_URL)
    assert sends >= 9  # about every 63 s; the old schedule gave 5 in 10 minutes


def test_a_restart_after_the_daily_read_times_does_not_fetch_again(cfg, clock, et) -> None:
    """Regression: DailyJob.last_day lived in memory only, so after a restart past 16:30 ET (or
    08:20) the daily job was due at once, the start read stepped aside for it, and Cboe and
    nowflation were fetched again with no check against the macro history."""
    from dataclasses import replace

    class Stamped(FakeFetcher):  # stamps each result with the clock, as the real fetcher does
        def fetch(self, source, url, **kw):
            got = super().fetch(source, url, **kw)
            stamp = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            return replace(got, started_at=stamp, ended_at=stamp)

    def _ctx(cfg, clock):
        return FakeCtx(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, fetch=Stamped(_results()))

    first = et("2026-11-02", "16:35")
    clock.set(first)
    ctx = _ctx(cfg, clock)
    run_due(build_jobs(ctx), first, LOG)
    urls = [u for _s, u in ctx.fetch.calls]
    assert urls.count(VIX_URL) == 1 and urls.count(CAL_URL) == 1
    later = et("2026-11-02", "16:45")
    clock.set(later)
    again = _ctx(cfg, clock)
    ran = run_due(build_jobs(again), later, LOG)
    urls = [u for _s, u in again.fetch.calls]
    assert urls.count(VIX_URL) == 0 and urls.count(CAL_URL) == 0
    assert "cboe_vix" not in ran and "nowflation@08:20" not in ran
    nxt = et("2026-11-03", "16:35")  # the next trading day's slot still reads
    clock.set(nxt)
    ctx3 = _ctx(cfg, clock)
    run_due(build_jobs(ctx3), nxt, LOG)
    assert [u for _s, u in ctx3.fetch.calls].count(VIX_URL) == 1
