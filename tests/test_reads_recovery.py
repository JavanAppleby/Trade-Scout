"""No selected pack ends without its line (review-scout F2, F3, F5 and Q2c; round 2, 2026-11-11):
a line that cannot be written, a restart on a later day, a packs file that became shorter, and a
reads file that cannot be read when the day is loaded. Mocked transport and ManualClock only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import scout.reads_service as service_mod
from conftest import make_config
from helpers_fake_anthropic import FakeAnthropic
from helpers_reads import DAY, answer, append_pack, pack, radar_dir, read_reply
from scout.clock import ManualClock
from scout.paths import ScoutPaths
from scout.reads_file import ReadsWriter, reads_on
from scout.reads_service import ReadsService
from scout.store import read_json

MADE = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)  # Monday 10:00 ET, CORE


class Rig:
    def __init__(self, scout_dir: Path, *replies: Any) -> None:
        self.radar = radar_dir(scout_dir)
        self.cfg = make_config(scout_dir, radar_dir="../Radar", llm_enabled=True, reads_enabled=True,
                               llm={"readers": {"R2": {"enabled": False}}})
        self.paths = ScoutPaths.from_config(self.cfg)
        self.clock = ManualClock(MADE + timedelta(seconds=5))
        self.tr = FakeAnthropic(*replies)

    def service(self) -> ReadsService:
        return ReadsService(self.cfg, self.paths, self.clock, self.tr, threaded=False)

    def cursor(self, day: str = DAY) -> int | None:
        rec = read_json(self.paths.reads / "_tail.json") or {}
        return (rec.get("files") or {}).get(f"{day}.packs.jsonl")


def _flaky(monkeypatch: pytest.MonkeyPatch, fails: int) -> dict[str, int]:
    real, seen = ReadsWriter.append, {"n": 0}

    def append(self: ReadsWriter, rec: dict, *, number: bool) -> dict:
        seen["n"] += 1
        if seen["n"] <= fails:
            raise PermissionError(13, "The process cannot access the file")
        return real(self, rec, number=number)

    monkeypatch.setattr(ReadsWriter, "append", append)
    monkeypatch.setattr(service_mod, "APPEND_PAUSE_S", 0.0)
    return seen


def test_a_line_that_fails_to_append_is_tried_again(scout_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(scout_dir, read_reply())
    seen = _flaky(monkeypatch, fails=2)
    append_pack(rig.radar, pack(seq=0, made_at=MADE))
    rig.service().tick(rig.clock.now())
    assert [x["kind"] for x in reads_on(rig.paths, DAY)] == ["first"] and seen["n"] == 3 and len(rig.tr.calls) == 1


def test_a_paid_read_whose_line_is_never_written_is_not_committed_past(scout_dir: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(scout_dir, read_reply(), read_reply())
    _flaky(monkeypatch, fails=service_mod.APPEND_TRIES)
    append_pack(rig.radar, pack(seq=0, made_at=MADE))
    rig.service().tick(rig.clock.now())
    assert reads_on(rig.paths, DAY) == [] and len(rig.tr.calls) == 1  # paid, but no line could be written
    assert rig.cursor() in (None, 0)  # ... so the position stays before the pack
    rig.service().tick(rig.clock.now())  # the next start polls it again, and now its line is written
    assert [x["kind"] for x in reads_on(rig.paths, DAY)] == ["first"]


def test_packs_left_at_a_stop_get_their_line_after_a_restart_on_a_later_day(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply())
    svc = rig.service()
    append_pack(rig.radar, pack(seq=0, made_at=MADE))
    svc.tick(rig.clock.now())  # read and committed
    late = datetime(2026, 11, 3, 0, 58, tzinfo=timezone.utc)  # Monday 19:58 ET, POST, trading day 2026-11-02
    append_pack(rig.radar, pack(symbol="WXYZ", candidate_id=8, seq=0, made_at=late, session="POST"))
    assert [p["candidate_id"] for p in svc.tailer.poll([MADE.date()], save=False)] == [8]  # then Ctrl+C
    rig.clock.set(datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc))  # Tuesday 08:00 ET: started again
    rig.service().tick(rig.clock.now())
    lines = reads_on(rig.paths, DAY)
    assert [(x["candidate_id"], x["kind"], x.get("why")) for x in lines] == [(7, "first", None), (8, "skip", "stale")]


def test_a_shorter_packs_file_is_read_again_and_no_pack_is_read_twice(scout_dir: Path) -> None:
    rig = Rig(scout_dir, *[read_reply() for _ in range(4)])
    svc = rig.service()
    p1, p2, p3 = (pack(symbol=f"S{c}", candidate_id=c, seq=0, made_at=MADE) for c in (1, 2, 3))
    for p in (p1, p2, p3):
        path = append_pack(rig.radar, p)
    svc.tick(rig.clock.now())
    path.write_bytes(b"")  # Radar's file rewritten, shorter: one old pack and one new one
    append_pack(rig.radar, p1)
    append_pack(rig.radar, pack(symbol="S4", candidate_id=4, seq=0, made_at=MADE))
    svc.tick(rig.clock.now())
    assert [x["candidate_id"] for x in reads_on(rig.paths, DAY)] == [1, 2, 3, 4] and len(rig.tr.calls) == 4
    assert rig.cursor() == path.stat().st_size


def test_an_unreadable_reads_file_does_not_load_the_day_empty(scout_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(scout_dir, read_reply(), read_reply(answer(status="intact")))
    append_pack(rig.radar, pack(seq=0, made_at=MADE))
    rig.service().tick(rig.clock.now())  # a first read: the thesis is alive
    (rig.paths.reads / "_tail.json").unlink()  # the next start polls the file from its start
    append_pack(rig.radar, pack(seq=1, made_at=MADE + timedelta(minutes=1)))
    rig.clock.set(MADE + timedelta(minutes=1, seconds=5))
    real, seen = service_mod.reads_on, {"n": 0}

    def locked_once(paths: Any, day: Any, *, strict: bool = False) -> list:
        seen["n"] += 1
        if seen["n"] == 1:
            raise PermissionError(13, "The process cannot access the file")
        return real(paths, day, strict=strict)

    monkeypatch.setattr(service_mod, "reads_on", locked_once)
    rig.service().tick(rig.clock.now())
    lines = reads_on(rig.paths, DAY)
    # 7:0 was not read twice (no empty "handled" set), and 7:1 loaded the day again and was reread
    assert [x["kind"] for x in lines] == ["first", "reread"] and len(rig.tr.calls) == 2
    assert lines[1]["parent_read_id"] == lines[0]["read_id"]
