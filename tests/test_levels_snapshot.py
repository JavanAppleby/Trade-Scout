"""The Jason snapshot and its job (DESIGN D4, 5.9, 8): written only when the merged current set
changes; the heartbeat carries the current files and the latest expiry."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from helpers_fakes import FakeBoard
from helpers_levels import FIXTURE_20260915, one_setup, plan_with, read, write_read

from scout import jobs_levels
from scout.clock import ManualClock, et_datetime
from scout.levels_snapshot import build_snapshot, latest_expiry, write_if_changed
from scout.models import JasonSnapshot
from scout.paths import ScoutPaths
from scout_reader.records import JASON_SNAPSHOTS, read_jsonl

NOW = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)


def _lines(paths: ScoutPaths) -> list[dict]:
    return read_jsonl(paths.root / JASON_SNAPSHOTS)


def test_snapshot_shape_regroups_readings(paths: ScoutPaths) -> None:
    write_read(paths.levels, plan_with([one_setup("A", [
        {"price": "730", "kind": "support", "ambiguous": True, "price_alternatives": ["7.30"], "note": "asr"},
        {"price": "130 to 125", "kind": "support"}, {"price": "220", "kind": "trigger"}],
        horizon="days")], expires_default="2026-09-19"))
    snap = build_snapshot(paths.levels, date(2026, 9, 16), NOW)
    rec = snap.to_record()
    assert rec["format"] == 1 and rec["first_seen_at"] == "2026-09-16T14:00:00Z" and rec["today_et"] == "2026-09-16"
    assert rec["files"] == ["2026-09-15.tuesday.json"]
    (setup,) = rec["setups"]
    assert setup == {"symbol": "A", "direction": "long", "attribution": "jason", "source_kind": "tuesday",
                     "session_date": "2026-09-15", "expires": "2026-09-19", "horizon": "days", "levels": [
                         {"kind": "support", "readings": ["7.30", "730"], "ambiguous": True, "note": "asr"},
                         {"kind": "support", "readings": ["125", "130"], "ambiguous": True, "note": ""},
                         {"kind": "trigger", "readings": ["220"], "ambiguous": False, "note": ""}]}
    assert JasonSnapshot.from_record(json.loads(json.dumps(rec))) == snap
    assert latest_expiry(snap) == "2026-09-19"


def test_snapshot_of_the_old_fixture(paths: ScoutPaths) -> None:
    (paths.levels / "2026-09-15.tuesday.json").write_bytes(FIXTURE_20260915.read_bytes())
    snap = build_snapshot(paths.levels, date(2026, 9, 16), NOW)
    spy = next(s for s in snap.setups if s["symbol"] == "SPY")
    assert {"kind": "support", "readings": ["7.30", "730"], "ambiguous": True,
            "note": "low end of support; ASR prints it 7:30"} in spy["levels"]
    assert snap.warnings == ("2026-09-15.tuesday.json: source_kind is missing - read as 'tuesday'",)


def test_written_only_when_the_set_changes(paths: ScoutPaths, write_audit) -> None:
    with write_audit.watching(paths.root):
        assert write_if_changed(paths, build_snapshot(paths.levels, date(2026, 9, 16), NOW)) is True  # first, empty
        assert write_if_changed(paths, build_snapshot(paths.levels, date(2026, 9, 16), NOW)) is False
        write_read(paths.levels, read())
        assert write_if_changed(paths, build_snapshot(paths.levels, date(2026, 9, 16), NOW)) is True
        later = NOW + timedelta(days=1)
        assert write_if_changed(paths, build_snapshot(paths.levels, date(2026, 9, 17), later)) is False  # same set
        assert write_if_changed(paths, build_snapshot(paths.levels, date(2026, 9, 20), later)) is True  # expired
    lines = _lines(paths)
    assert [len(r["setups"]) for r in lines] == [0, 4, 0]
    assert lines[1]["files"] == ["2026-09-15.tuesday.json"]


# --------------------------------------------------------------------------- the job


def _ctx(paths: ScoutPaths, clock: ManualClock) -> SimpleNamespace:
    return SimpleNamespace(paths=paths, clock=clock, board=FakeBoard(), log=logging.getLogger("test.jobs_levels"))


def test_job_runs_at_start_then_on_a_change_and_daily(paths: ScoutPaths) -> None:
    start = et_datetime(date(2026, 9, 16), datetime.min.time().replace(hour=10))
    clock = ManualClock(start)
    ctx = _ctx(paths, clock)
    (job,) = jobs_levels.build_jobs(ctx)
    assert job.name == "jason_snapshot"
    assert job.due(clock.now())
    job.run(clock.now())
    assert ctx.board.levels == {"current_files": [], "expires": None}
    assert len(_lines(paths)) == 1

    clock.advance(60)
    assert not job.due(clock.now())
    write_read(paths.levels, read())
    clock.advance(jobs_levels.CHECK_INTERVAL_S)
    assert job.due(clock.now())
    job.run(clock.now())
    assert ctx.board.levels == {"current_files": ["2026-09-15.tuesday.json"], "expires": "2026-09-19"}
    clock.advance(jobs_levels.CHECK_INTERVAL_S)
    assert not job.due(clock.now())  # nothing changed

    clock.set(et_datetime(date(2026, 9, 17), jobs_levels.DAILY_AT_ET) - timedelta(minutes=1))
    assert not job.due(clock.now())
    clock.advance(60)
    assert job.due(clock.now())
    job.run(clock.now())
    assert len(_lines(paths)) == 2  # the daily run found the same set and wrote nothing

    clock.set(et_datetime(date(2026, 9, 20), jobs_levels.DAILY_AT_ET) + timedelta(minutes=1))
    assert job.due(clock.now())
    job.run(clock.now())
    assert ctx.board.levels == {"current_files": [], "expires": None}
    assert len(_lines(paths)) == 3


def test_folder_signature_ignores_sidecars(paths: ScoutPaths) -> None:
    empty = jobs_levels.folder_signature(paths.levels)
    (paths.levels / "x.meta.json").write_text("{}", encoding="utf-8")
    assert jobs_levels.folder_signature(paths.levels) == empty
    write_read(paths.levels, read())
    assert jobs_levels.folder_signature(paths.levels) != empty
    assert jobs_levels.folder_signature(Path(paths.root / "missing")) == ()


def test_the_app_loads_the_levels_job(scout_dir: Path) -> None:
    from conftest import make_config

    from scout.app import AppContext, load_jobs
    from scout.heartbeat import SourceStatusBoard

    cfg = make_config(scout_dir)
    ctx = AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=ManualClock(NOW),
                     board=SourceStatusBoard(cfg), log=logging.getLogger("test.app"))
    jobs = load_jobs(ctx, modules=("scout.jobs_levels",), strict=True)
    assert [j.name for j in jobs] == ["jason_snapshot"]
    jobs[0].run(NOW)
    assert ctx.board.levels == {"current_files": [], "expires": None}
