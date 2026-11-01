"""The heartbeat (SCOUT_SPEC 10.1, DESIGN 5.3)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.clock import ManualClock, iso
from scout.config import ScoutConfig
from scout.heartbeat import Heartbeat, SourceStatusBoard, build_body
from scout.models import Beat, HeartbeatBody, SourceState
from scout.paths import ScoutPaths
from scout_reader.records import governing_beat, read_jsonl

SS101_KEYS = {"format", "at", "version", "stopped", "sources", "llm", "levels", "calendar_covers_next_45d"}


def _read(paths: ScoutPaths) -> dict:
    return json.loads(paths.heartbeat_file.read_text(encoding="utf-8"))


def test_beat_writes_the_spec_shape_and_a_beat_line(paths: ScoutPaths, cfg: ScoutConfig, clock: ManualClock) -> None:
    board = SourceStatusBoard(cfg)
    board.update("edgar_atom", SourceState(enabled=True, status="ok", last_ok=iso(clock.now()), gap_open=False))
    board.set_levels(["2026-11-03.tuesday.json"], "2026-11-10")
    hb = Heartbeat(paths, cfg, board, clock)
    assert hb.beat(clock.now())
    body = _read(paths)
    assert set(body) == SS101_KEYS and body["format"] == 1 and body["stopped"] is False
    assert body["at"] == "2026-11-02T14:00:00Z" and body["version"] == "0.1.0"
    assert body["sources"]["edgar_atom"] == {"enabled": True, "status": "ok", "last_ok": "2026-11-02T14:00:00Z",
                                             "last_error": None, "gap_open": False}
    assert body["sources"]["prnewswire"]["status"] == "starting"
    assert body["llm"] == {"enabled": False, "spent_today_usd": 0.0, "cap_reached": False}
    assert body["levels"] == {"current_files": ["2026-11-03.tuesday.json"], "expires": "2026-11-10"}
    assert body["calendar_covers_next_45d"] is True
    HeartbeatBody.from_record(body)
    beats = read_jsonl(paths.beats / "2026-11-02.jsonl")
    assert len(beats) == 1 and Beat.from_record(beats[0]).sources["edgar_atom"] == "ok"
    assert governing_beat(paths.root, clock.now())["at"] == "2026-11-02T14:00:00Z"


def test_due_every_interval(paths: ScoutPaths, cfg: ScoutConfig, clock: ManualClock) -> None:
    hb = Heartbeat(paths, cfg, SourceStatusBoard(cfg), clock)
    assert hb.due(clock.now())
    hb.beat(clock.now())
    assert not hb.due(clock.now() + timedelta(seconds=29))
    assert hb.due(clock.now() + timedelta(seconds=30))


def test_starting_and_stopped_make_readers_stale(paths: ScoutPaths, cfg: ScoutConfig, clock: ManualClock) -> None:
    hb = Heartbeat(paths, cfg, SourceStatusBoard(cfg), clock)
    hb.beat(clock.now())
    clock.advance(10)
    hb.stopped(clock.now())
    body = _read(paths)
    assert body["stopped"] is True and body["sources"] == {}
    assert governing_beat(paths.root, clock.now()) is None  # the latest beat says stopped
    hb.starting(clock.now() + timedelta(seconds=1))
    assert _read(paths)["stopped"] is True


def test_calendar_coverage_flag(paths: ScoutPaths, cfg: ScoutConfig) -> None:
    board = SourceStatusBoard(cfg)
    assert build_body(datetime(2027, 12, 1, 14, tzinfo=timezone.utc), board).calendar_covers_next_45d is False
    assert build_body(datetime(2027, 11, 1, 14, tzinfo=timezone.utc), board).calendar_covers_next_45d is True


def test_beat_never_raises(paths: ScoutPaths, cfg: ScoutConfig, clock: ManualClock,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    import scout.heartbeat as mod

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise OSError("disk full")

    monkeypatch.setattr(mod, "atomic_write_json", boom)
    hb = Heartbeat(paths, cfg, SourceStatusBoard(cfg), clock)
    assert hb.beat(clock.now()) is False and hb.last_at is None


def test_disabled_source_shows_disabled(scout_dir, clock: ManualClock) -> None:  # noqa: ANN001
    from conftest import make_config  # noqa: PLC0415

    cfg = make_config(scout_dir, sources={"bls": {"enabled": False}})
    body = build_body(clock.now(), SourceStatusBoard(cfg))
    assert body.sources["bls"]["status"] == "disabled" and body.sources["bls"]["enabled"] is False


def test_a_beat_while_the_loop_is_busy_shows_the_gap(paths: ScoutPaths, cfg: ScoutConfig) -> None:
    """Regression: a Pulse beat copied the last poll's ok / no gap, so while the main loop sat in a
    long job, halt_open answered 'false' on a feed nobody was polling. A polled feed whose last good
    poll is older than its gap limit is now published with gap_open true."""
    from scout_reader.halts import halt_open_from

    t0 = datetime(2026, 11, 2, 15, 0, 5, tzinfo=timezone.utc)  # 10:00:05 ET
    clock = ManualClock(t0)
    board = SourceStatusBoard(cfg)
    board.update("nasdaq_halts", SourceState(enabled=True, status="ok", last_ok=iso(t0), gap_open=False))
    board.update("prnewswire", SourceState(enabled=True, status="ok", last_ok=iso(t0), gap_open=False))
    board.update("bls", SourceState(enabled=True, status="ok", last_ok=iso(t0 - timedelta(days=3))))
    hb = Heartbeat(paths, cfg, board, clock)
    assert hb.beat(t0 + timedelta(seconds=60))
    beat = governing_beat(paths.root, t0 + timedelta(seconds=60))
    assert beat["gap_open"]["nasdaq_halts"] is False
    late = t0 + timedelta(minutes=10)
    assert hb.beat(late)
    body = _read(paths)
    assert body["sources"]["nasdaq_halts"]["gap_open"] is True and body["sources"]["nasdaq_halts"]["status"] == "ok"
    assert body["sources"]["prnewswire"]["gap_open"] is True
    assert body["sources"]["bls"]["gap_open"] is False  # not a polled feed: its records carry stale_after
    beat = governing_beat(paths.root, late)
    assert beat["gap_open"]["nasdaq_halts"] is True
    assert halt_open_from([], beat, "ABC", late)["value"] == "unknown"
    assert board.sources["nasdaq_halts"].gap_open is False  # the board itself is not changed


def test_the_halt_feed_is_not_aged_outside_active_hours(cfg: ScoutConfig) -> None:
    board = SourceStatusBoard(cfg)
    last = datetime(2026, 11, 3, 0, 59, tzinfo=timezone.utc)  # 19:59 ET
    board.update("nasdaq_halts", SourceState(enabled=True, status="ok", last_ok=iso(last)))
    body = build_body(last + timedelta(hours=2), board)
    assert body.sources["nasdaq_halts"]["gap_open"] is False
