"""Context, params and the main loop (DESIGN 2.1, 8)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from scout.app import AppContext, load_jobs, run, startup_warnings, write_params
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.heartbeat import SourceStatusBoard
from scout.paths import ScoutPaths
from scout.running import ScoutLock, is_running
from scout.scheduler import EveryJob
from scout_reader.records import governing_beat, last_params, read_jsonl


@pytest.fixture
def ctx(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> AppContext:
    return AppContext(cfg=cfg, paths=paths, clock=clock, board=SourceStatusBoard(cfg),
                      log=logging.getLogger("test.app"))


def test_run_beats_writes_params_and_stops(ctx: AppContext, clock: ManualClock) -> None:
    ticks = []
    job = EveryJob("probe", ticks.append, 10)
    assert run(ctx, max_ticks=65, jobs=[job]) == 0
    beats = read_jsonl(ctx.paths.beats / "2026-11-02.jsonl")
    assert beats[0]["stopped"] is True and beats[-1]["stopped"] is True  # starting ... stopped
    assert sum(1 for b in beats if not b["stopped"]) == 3  # 65 s at 30 s spacing: t=0, 30, 60
    assert len(ticks) == 7
    assert governing_beat(ctx.paths.root, clock.now()) is None  # stopped: readers see Scout as stale
    params = last_params(ctx.paths.root, clock.now())
    assert params is not None and params["scout_stale_s"] == 120 and len(params["config_sha256"]) == 64
    assert "test@example.invalid" not in ctx.paths.params_file.read_text(encoding="utf-8")


def test_params_appended_only_on_change(ctx: AppContext, scout_dir: Path, clock: ManualClock) -> None:
    assert write_params(ctx) is True
    assert write_params(ctx) is False
    from conftest import make_config  # noqa: PLC0415

    ctx.cfg = make_config(scout_dir, scout_stale_s=90)
    clock.advance(5)
    assert write_params(ctx) is True
    assert [r["scout_stale_s"] for r in read_jsonl(ctx.paths.params_file)] == [120, 90]


@pytest.mark.parametrize("name", ["STOP", "STOP.txt"])
def test_stop_file_stops_the_loop_and_is_deleted(ctx: AppContext, name: str) -> None:
    """Regression (third review): STOP was never deleted, so every later start stopped at once,
    and the STOP.txt that Explorer makes (extensions hidden) never stopped Scout."""
    ticks: list[object] = []

    def probe(now: object) -> None:
        ticks.append(now)
        if len(ticks) == 3:
            (ctx.paths.root / name).write_text("", encoding="utf-8")

    assert run(ctx, max_ticks=1000, jobs=[EveryJob("probe", probe, 0)]) == 0
    assert len(ticks) == 3
    assert not (ctx.paths.root / name).exists()
    beats = read_jsonl(ctx.paths.beats / "2026-11-02.jsonl")
    assert beats[0]["stopped"] is True and beats[-1]["stopped"] is True


def test_a_stop_file_left_before_the_start_is_deleted_and_scout_runs(ctx: AppContext) -> None:
    """Regression (third review): a STOP left in the folder stopped every start at once."""
    ctx.paths.stop_file.write_text("", encoding="utf-8")
    ticks: list[object] = []
    assert run(ctx, max_ticks=5, jobs=[EveryJob("probe", ticks.append, 0)]) == 0
    assert len(ticks) == 5 and not ctx.paths.stop_file.exists()


def test_second_scout_refused(ctx: AppContext, capsys: pytest.CaptureFixture[str]) -> None:
    with ScoutLock(ctx.paths):
        assert is_running(ctx.paths.root)
        assert run(ctx, max_ticks=1, jobs=[]) == 4
    assert "Scout did not start" in capsys.readouterr().out
    assert not is_running(ctx.paths.root)


def test_load_jobs_skips_units_not_built(ctx: AppContext) -> None:
    assert load_jobs(ctx, modules=("scout.jobs_not_a_module",)) == []
    with pytest.raises(ModuleNotFoundError):
        load_jobs(ctx, modules=("scout.jobs_not_a_module",), strict=True)


def test_startup_warnings(ctx: AppContext, clock: ManualClock) -> None:
    text = " ".join(startup_warnings(ctx, clock.now()))
    assert "radar_dir is empty" in text and "No Jason levels" in text and "contact_email" not in text


def test_a_start_after_1930_answers_with_a_live_beat_not_stale(scout_dir: Path) -> None:
    """Regression: started at 19:40 ET, the answers jobs ran on the first tick, when the only beat
    was "starting" (stopped: true); every answer was stale/unknown and the job was done for the day.
    They now wait for the main loop's first live beat."""
    from conftest import et_at, make_config  # noqa: PLC0415

    from scout.jobs_answers import build_jobs as answer_jobs  # noqa: PLC0415

    cfg = make_config(scout_dir, watch_symbols=["ABCD"])
    clock = ManualClock(et_at("2026-11-02", "19:40"))
    ctx = AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, board=SourceStatusBoard(cfg),
                     log=logging.getLogger("test.app"))
    jobs = answer_jobs(ctx)
    assert run(ctx, max_ticks=1, jobs=jobs) == 0
    answers_file = ctx.paths.answers / "2026-11-02.jsonl"
    assert not answers_file.exists()  # nothing written before a live beat, and nothing marked done
    assert run(ctx, max_ticks=2, jobs=jobs) == 0
    answers = read_jsonl(answers_file)
    assert answers and all(a["symbol"] == "ABCD" for a in answers)
    assert all(a["stale"] is False and "scout_stale" not in a["reasons"]["fresh_catalyst"] for a in answers)


def test_pulse_beats_while_a_job_runs_long(ctx: AppContext, clock: ManualClock) -> None:
    """Regression: the beat was written only between ticks, so one slow host (10 forms x 20 s) or
    a 120 s Claude call left no beat for over scout_stale_s and every reader saw Scout as down."""
    import time  # noqa: PLC0415

    from scout.heartbeat import Heartbeat, Pulse  # noqa: PLC0415

    hb = Heartbeat(ctx.paths, ctx.cfg, ctx.board, clock)
    pulse = Pulse(hb, clock, check_s=0.01)
    assert pulse.tick() is False  # no live beat from the loop yet: the pulse waits
    ctx.paths.ensure()
    hb.beat(clock.now())
    pulse.start()
    try:
        for _step in range(8):  # a 240 s job, in 30 s steps of the Scout clock
            clock.advance(30)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and hb.last_at != clock.now():
                time.sleep(0.005)
            assert hb.last_at == clock.now()
    finally:
        pulse.stop()
    beats = [b for b in read_jsonl(ctx.paths.beats / "2026-11-02.jsonl") if not b["stopped"]]
    assert len(beats) == 9
    assert governing_beat(ctx.paths.root, clock.now()) is not None


def test_run_with_the_pulse_stops_it_before_the_stopped_beat(ctx: AppContext) -> None:
    assert run(ctx, max_ticks=3, jobs=[], pulse=True) == 0
    beats = read_jsonl(ctx.paths.beats / "2026-11-02.jsonl")
    assert beats[-1]["stopped"] is True and ctx.live_beat_at is None



def test_the_log_file_rolls_over_at_et_midnight(ctx: AppContext, clock: ManualClock) -> None:
    """Regression: setup_logging ran once, so a Scout left running all week wrote every day into
    the first day's file (DESIGN 2.1: one log file per ET date)."""
    from conftest import et_at  # noqa: PLC0415

    from scout.clock import et_date  # noqa: PLC0415
    from scout.logs import close_logging, setup_logging  # noqa: PLC0415

    clock.set(et_at("2026-11-02", "23:59"))
    log = setup_logging(ctx.paths, "INFO", et_date(clock.now()), ctx.cfg, today=lambda: et_date(clock.now()))
    try:
        log.info("monday line")
        clock.advance(120)
        log.info("tuesday line")
    finally:
        close_logging()
    monday = (ctx.paths.logs / "scout-2026-11-02.log").read_text(encoding="utf-8")
    tuesday = (ctx.paths.logs / "scout-2026-11-03.log").read_text(encoding="utf-8")
    assert "monday line" in monday and "tuesday line" not in monday and "tuesday line" in tuesday


def test_log_lines_show_et_with_the_zone() -> None:
    """Regression (third review): lines used the PC's local time (Brisbane) with no zone, while the
    file name, the brief and the heartbeat use ET."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from scout.logs import _FORMAT, EtFormatter  # noqa: PLC0415

    record = logging.LogRecord("scout", logging.INFO, __file__, 1, "brief written", None, None)
    record.created = datetime(2026, 11, 3, 13, 20, 0, tzinfo=timezone.utc).timestamp()  # 08:20 ET (EST)
    record.msecs = 5
    assert EtFormatter(_FORMAT).format(record).startswith("2026-11-03 08:20:00,005 ET INFO scout: brief written")
    record.created = datetime(2026, 7, 1, 12, 20, 0, tzinfo=timezone.utc).timestamp()  # 08:20 ET (EDT)
    assert EtFormatter(_FORMAT).format(record).startswith("2026-07-01 08:20:00,005 ET")
