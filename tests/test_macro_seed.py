"""The macro sources after a restart (scout.macro_seed; DESIGN 2.3 "Restart").

Regression (live, Sunday 2026-11-01 02:12 ET): after a restart the heartbeat showed bls and cboe_vix
as "starting". Their jobs were seeded done from the macro history, so they were not read again, but
the board was not seeded, so it stayed "starting" until the next read (up to a week for BLS) and
every answer depending on them was unknown (SS 10.1). And Cboe's first start read (Saturday 23:18
ET) had failed, yet that failed line counted as the slot's read, so the restart did not read again.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from conftest import TEST_EMAIL, et_at
from helpers_fakes import FakeFetcher, FakeResponse
from helpers_macro import BLS_URL, CAL_URL, HALTS_URL, VIX_URL, FakeCtx, halts_body, macro_body, nowflation_results
from helpers_wire import full_transport

from scout.app import build_context, load_jobs, run
from scout.clock import ManualClock
from scout.http import HttpFetcher
from scout.jobs_macro import build_jobs
from scout.macro_seed import SEEDED, last_read, seeded_state
from scout.paths import ScoutPaths
from scout.scheduler import run_due
from scout.store import read_jsonl
from scout_reader import Reader

LOG = logging.getLogger("scout.test.macro_seed")
NOT_SEEDED = ("nasdaq_halts", "edgar_atom", "prnewswire", "edgar_submissions")


def _results() -> dict:
    return {HALTS_URL: halts_body(), BLS_URL: macro_body("bls_SYNTHETIC.ics"),
            VIX_URL: macro_body("VIX_History_head_SYNTHETIC.csv"), **nowflation_results()}


class Stamped(FakeFetcher):
    """Stamps each result with the clock, as the real fetcher does."""

    def __init__(self, clock: ManualClock, results: dict) -> None:
        super().__init__(results)
        self.clock = clock

    def fetch(self, source, url, **kw):
        got = super().fetch(source, url, **kw)
        stamp = self.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        return replace(got, started_at=stamp, ended_at=stamp)


def _start(cfg, clock, at, *, drop: tuple[str, ...] = (), only: tuple[str, ...] | None = None):
    """A Scout start at `at` in the same folder: new jobs, new board. Returns (ctx, jobs)."""
    clock.set(at)
    results = {u: b for u, b in _results().items() if u not in drop}
    ctx = FakeCtx(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, fetch=Stamped(clock, results))
    jobs = build_jobs(ctx)
    if only is not None:
        jobs = [j for j in jobs if j.name in only]
    return ctx, jobs


def _urls(ctx) -> list[str]:
    return [u for _s, u in ctx.fetch.calls]


def _history(cfg) -> list[dict]:
    return read_jsonl(ScoutPaths.from_config(cfg).macro / "history.jsonl")


# --------------------------------------------------------------------------- BLS


def test_a_restart_mid_week_after_a_good_bls_read_shows_ok(cfg, clock) -> None:
    ctx, jobs = _start(cfg, clock, et_at("2026-11-02", "09:00"))
    run_due(jobs, clock.now(), LOG)
    read_at = next(h for h in _history(cfg) if h["source"] == "bls")["first_seen_at"]
    again, jobs = _start(cfg, clock, et_at("2026-11-05", "10:00"))  # Thursday
    state = again.board.sources["bls"]  # seeded at build time, before any read
    assert state.status == "ok" and state.last_ok == read_at and state.last_error is None
    run_due(jobs, clock.now(), LOG)
    assert BLS_URL not in _urls(again)  # still weekly
    assert again.board.sources["bls"].status == "ok"


def test_a_restart_after_the_bls_record_expired_is_stale_never_ok(cfg, clock) -> None:
    ctx, jobs = _start(cfg, clock, et_at("2026-11-02", "09:00"), only=("bls",))
    run_due(jobs, clock.now(), LOG)
    again, _jobs = _start(cfg, clock, et_at("2026-11-17", "09:00"))  # past fetched + 2 weeks
    state = again.board.sources["bls"]
    assert state.status == "stale" and state.last_error == "stale_after_passed"
    assert state.last_ok is not None


def test_a_bls_record_with_no_nfp_ahead_is_stale_on_restart(cfg, et) -> None:
    rec = {"source": "bls", "status": "ok", "reasons": [], "fetched_at": "2026-11-02T14:00:00Z",
           "stale_after": "2026-11-16T14:00:00Z",
           "events": [{"kind": "cpi", "event_at": "2026-11-12T13:30:00Z"},
                      {"kind": "nfp", "event_at": "2026-11-06T13:30:00Z"}]}
    assert seeded_state(cfg, rec, "2026-11-02T14:00:00Z", et("2026-11-05", "09:00")).status == "ok"
    state = seeded_state(cfg, rec, "2026-11-02T14:00:00Z", et("2026-11-09", "09:00"))  # the NFP has passed
    assert state.status == "stale" and state.last_error == "no_cpi_and_nfp_after_now"


def test_a_restart_after_a_failed_bls_read_shows_unavailable_and_reads_once(cfg, clock) -> None:
    ctx, jobs = _start(cfg, clock, et_at("2026-11-02", "09:00"), only=("bls",))
    run_due(jobs, clock.now(), LOG)  # a good read on Monday
    good_at = _history(cfg)[-1]["first_seen_at"]
    ctx, jobs = _start(cfg, clock, et_at("2026-11-09", "10:00"), drop=(BLS_URL,), only=("bls",))
    run_due(jobs, clock.now(), LOG)  # a week later the weekly read fails
    assert _history(cfg)[-1]["status"] == "unavailable"
    again, jobs = _start(cfg, clock, et_at("2026-11-10", "10:00"), drop=(BLS_URL,), only=("bls",))
    state = again.board.sources["bls"]
    assert state.status == "unavailable" and state.last_error == "fetch:network" and state.last_ok == good_at
    run_due(jobs, clock.now(), LOG)  # the failed line did not use up the week: one read at start
    assert _urls(again) == [BLS_URL]
    assert again.board.sources["bls"].status == "unavailable"
    assert again.board.sources["bls"].last_ok == good_at  # the reader's last_ok was seeded too
    run_due(jobs, clock.now() + timedelta(hours=2), LOG)  # within the run: no retry before the week
    assert _urls(again) == [BLS_URL]
    third, jobs = _start(cfg, clock, et_at("2026-11-10", "11:00"), only=("bls",))
    run_due(jobs, clock.now(), LOG)  # the next restart reads again and recovers
    assert _urls(third) == [BLS_URL] and third.board.sources["bls"].status == "ok"


# --------------------------------------------------------------------------- Cboe


def test_a_restart_after_the_cboe_read_time_on_a_trading_day(cfg, clock) -> None:
    ctx, jobs = _start(cfg, clock, et_at("2026-11-02", "16:35"))
    run_due(jobs, clock.now(), LOG)
    assert _urls(ctx).count(VIX_URL) == 1
    again, jobs = _start(cfg, clock, et_at("2026-11-02", "16:50"))
    assert again.board.sources["cboe_vix"].status == "ok"
    ran = run_due(jobs, clock.now(), LOG)
    assert VIX_URL not in _urls(again) and "cboe_vix" not in ran
    assert again.board.sources["cboe_vix"].status == "ok"
    # the next morning the fixture's last close (Friday) is older than Monday: stale, never ok
    nxt, _jobs = _start(cfg, clock, et_at("2026-11-03", "10:00"))
    assert nxt.board.sources["cboe_vix"].status == "stale"


def test_a_weekend_restart_after_fridays_read_shows_ok(cfg, clock) -> None:
    ctx, jobs = _start(cfg, clock, et_at("2026-10-30", "16:45"), only=("cboe_vix_start", "cboe_vix"))
    run_due(jobs, clock.now(), LOG)
    assert _history(cfg)[-1]["status"] == "ok"
    again, jobs = _start(cfg, clock, et_at("2026-11-01", "02:12"))
    assert again.board.sources["cboe_vix"].status == "ok"
    run_due(jobs, clock.now(), LOG)
    assert VIX_URL not in _urls(again)


def test_a_failed_saturday_night_cboe_read_does_not_block_the_restart(cfg, clock) -> None:
    """The live case: the first start on Saturday 23:18 ET failed (network); the restart at Sunday
    02:12 ET found that line counted as the read for Friday's slot and did not read until Monday."""
    ctx, jobs = _start(cfg, clock, et_at("2026-10-31", "23:18"), drop=(VIX_URL,))
    run_due(jobs, clock.now(), LOG)
    assert _urls(ctx).count(VIX_URL) == 1
    assert [h for h in _history(cfg) if h["source"] == "cboe_vix"][-1]["status"] == "unavailable"
    assert last_read(type("W", (), {"latest": {"cboe_vix": _history(cfg)[-1]}})(), "cboe_vix") is None
    again, jobs = _start(cfg, clock, et_at("2026-11-01", "02:12"))
    assert again.board.sources["cboe_vix"].status == "unavailable"  # seeded: never ok from a failure
    run_due(jobs, clock.now(), LOG)
    assert _urls(again).count(VIX_URL) == 1
    assert again.board.sources["cboe_vix"].status == "ok"
    third, jobs = _start(cfg, clock, et_at("2026-11-01", "03:00"))  # that read now counts
    run_due(jobs, clock.now(), LOG)
    assert VIX_URL not in _urls(third) and third.board.sources["cboe_vix"].status == "ok"


def test_a_failed_read_after_the_daily_time_does_not_mark_the_daily_job_done(cfg, clock) -> None:
    ctx, jobs = _start(cfg, clock, et_at("2026-11-02", "16:35"), drop=(VIX_URL,))
    run_due(jobs, clock.now(), LOG)
    again, jobs = _start(cfg, clock, et_at("2026-11-02", "17:00"))
    ran = run_due(jobs, clock.now(), LOG)
    assert "cboe_vix" in ran and _urls(again).count(VIX_URL) == 1  # the daily read, once
    assert again.board.sources["cboe_vix"].status == "ok"


# --------------------------------------------------------------------------- nowflation and the rest


def test_nowflation_is_seeded_and_rechecked(cfg, clock, et) -> None:
    ctx, jobs = _start(cfg, clock, et_at("2026-11-02", "07:00"))
    run_due(jobs, clock.now(), LOG)
    again, jobs = _start(cfg, clock, et_at("2026-11-02", "07:30"))
    assert again.board.sources["nowflation"].status == "ok"
    run_due(jobs, clock.now(), LOG)
    assert CAL_URL not in _urls(again)
    rec = next(h for h in reversed(_history(cfg)) if h["source"] == "nowflation")
    later = seeded_state(cfg, rec, rec["first_seen_at"], et("2026-11-03", "09:30"))  # not today's after 09:15
    assert later.status == "stale" and later.last_error


def test_a_stale_line_stays_stale_and_polled_feeds_are_not_seeded(cfg, clock, et) -> None:
    rec = {"source": "nowflation", "status": "stale", "reasons": ["last_good_held"], "fetched_at": "x",
           "source_generated_at": "2026-11-02T11:05:00Z", "stale_after": None}
    state = seeded_state(cfg, rec, None, et("2026-11-02", "07:00"))
    assert state.status == "stale" and state.last_error == "last_good_held" and state.last_ok is None
    ctx, jobs = _start(cfg, clock, et_at("2026-11-02", "09:00"))
    run_due(jobs, clock.now(), LOG)
    again, _jobs = _start(cfg, clock, et_at("2026-11-02", "09:10"))
    assert set(again.board.sources) <= set(SEEDED)
    assert not any(name in again.board.sources for name in NOT_SEEDED)


def test_a_history_line_from_after_now_is_never_ok(cfg, et) -> None:
    """The clock went back (or a history copied from another machine): a line read, or data
    generated, more than FUTURE_SLACK after now is not fresh, whatever its stale_after says."""
    now = et("2026-11-05", "09:00")
    bls = {"source": "bls", "status": "ok", "reasons": [], "first_seen_at": "2026-11-20T14:00:00Z",
           "fetched_at": "2026-11-20T14:00:00Z", "stale_after": "2026-12-04T14:00:00Z",
           "events": [{"kind": "cpi", "event_at": "2026-12-10T13:30:00Z"},
                      {"kind": "nfp", "event_at": "2026-12-04T13:30:00Z"}]}
    state = seeded_state(cfg, bls, bls["first_seen_at"], now)
    assert state.status == "stale" and state.last_error == "read_after_now"
    nf = {"source": "nowflation", "status": "ok", "reasons": [], "first_seen_at": "2026-11-05T13:30:00Z",
          "fetched_at": "2026-11-05T13:30:00Z", "source_generated_at": "2026-11-05T19:00:00Z",
          "stale_after": "2026-11-06T14:15:00Z"}  # read now, but generated hours ahead of the clock
    state = seeded_state(cfg, nf, nf["first_seen_at"], now)
    assert state.status == "stale" and state.last_error == "generated_in_future"
    vix = {"source": "cboe_vix", "status": "ok", "reasons": [], "first_seen_at": "2026-11-06T21:31:00Z",
           "fetched_at": "2026-11-06T21:31:00Z", "stale_after": "2026-11-09T22:30:00Z",
           "vix": {"date": "2026-11-06", "close": 18.0}}
    assert seeded_state(cfg, vix, vix["first_seen_at"], now).status == "stale"
    late = dict(bls, first_seen_at="2026-11-05T14:30:00Z", fetched_at="2026-11-05T14:30:00Z")
    assert seeded_state(cfg, late, None, now).status == "ok"  # within the slack: a read seconds ago
    assert seeded_state(cfg, dict(late, stale_after=None), None, now).last_error == "no_stale_after"


def test_a_damaged_history_line_is_stale_and_scout_still_starts(cfg, clock) -> None:
    """A history line with a field of the wrong type must not stop build_jobs (the app would not
    start) and must never seed ok."""
    from scout import FORMAT
    clock.set(et_at("2026-11-05", "10:00"))
    macro = ScoutPaths.from_config(cfg).macro
    macro.mkdir(parents=True, exist_ok=True)
    lines = [{"format": FORMAT, "source": "bls", "status": "ok", "reasons": [], "events": 5,
              "first_seen_at": "2026-11-02T14:00:00Z", "fetched_at": "2026-11-02T14:00:00Z",
              "stale_after": "2026-11-16T14:00:00Z"},
             {"format": FORMAT, "source": "cboe_vix", "status": "ok", "reasons": 5, "vix": None,
              "first_seen_at": "2026-11-04T21:31:00Z", "fetched_at": "2026-11-04T21:31:00Z",
              "stale_after": "2026-11-05T22:30:00Z"}]
    (macro / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    ctx = FakeCtx(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, fetch=Stamped(clock, _results()))
    build_jobs(ctx)
    for source in ("bls", "cboe_vix"):
        state = ctx.board.sources[source]
        assert state.status == "stale" and state.last_error == "history_unreadable"


def test_a_disabled_source_is_not_seeded(scout_dir, clock) -> None:
    from conftest import make_config

    cfg = make_config(scout_dir)
    ctx, jobs = _start(cfg, clock, et_at("2026-11-02", "09:00"))
    run_due(jobs, clock.now(), LOG)
    off = make_config(scout_dir, sources={"bls": {"enabled": False}})
    again, _jobs = _start(off, clock, et_at("2026-11-02", "09:10"))
    assert "bls" not in again.board.sources and again.board.sources["cboe_vix"].status == "ok"


# --------------------------------------------------------------------------- end to end


def _transport(cfg):
    tr = full_transport(cfg)
    for url, body in _results().items():
        tr.responses[url] = FakeResponse(200, {}, body)
    return tr


def _run(folder: Path, start: str, end: str):
    clock = ManualClock(et_at("2026-11-02", start))
    ctx = build_context(folder / "config.toml", clock=clock, log=logging.getLogger("scout.test.seed_e2e"))
    http = _transport(ctx.cfg)
    ctx._services["fetcher"] = HttpFetcher(ctx.cfg, clock, transport=http, log=ctx.log.getChild("http"))
    assert run(ctx, until=et_at("2026-11-02", end), jobs=load_jobs(ctx, strict=True)) == 0
    return [url for url, *_ in http.calls]


def test_e2e_a_restart_publishes_the_macro_sources_as_ok_and_answers_stay_known(tmp_path: Path) -> None:
    folder = tmp_path / "Scout"
    folder.mkdir()
    (folder / "config.toml").write_text(f'contact_email = "{TEST_EMAIL}"\nloop_interval_s = 10.0\n',
                                        encoding="utf-8")
    first = _run(folder, "16:28", "16:36")  # reads BLS, nowflation, Cboe (16:30)
    assert BLS_URL in first and VIX_URL in first and CAL_URL in first
    second = _run(folder, "17:00", "17:03")  # the restart
    assert BLS_URL not in second and VIX_URL not in second and CAL_URL not in second
    beats = read_jsonl(folder / "state" / "beats" / "2026-11-02.jsonl")
    live = [b for b in beats if not b.get("stopped") and b["at"] >= "2026-11-02T22:00:00Z"]
    assert live
    for source in ("bls", "cboe_vix", "nowflation"):
        assert live[0]["sources"][source] == "ok", source  # the first beat after the restart
    hb = json.loads((folder / "state" / "scout.heartbeat").read_text(encoding="utf-8"))
    assert hb["stopped"] is True
    as_of = et_at("2026-11-02", "17:02")
    ctx = Reader(folder, now=lambda: as_of).market_context(as_of)
    assert ctx["vix"]["value"] == "true" and ctx["inflation_regime"]["value"] == "true"
