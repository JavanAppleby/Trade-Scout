"""Tailing Radar's packs file (READS_TEST.md 4.1, 5): new complete lines only, never an old pack again."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from conftest import make_config
from helpers_reads import DAY, append_pack, pack, radar_dir
from scout.paths import ScoutPaths
from scout.reads_tail import PackTailer, open_shared, packs_file

D = date.fromisoformat(DAY)


def _tailer(scout_dir: Path) -> tuple[PackTailer, Path]:
    radar = radar_dir(scout_dir)
    cfg = make_config(scout_dir, radar_dir="../Radar")
    return PackTailer(cfg, ScoutPaths.from_config(cfg)), radar


def test_reads_new_lines_and_leaves_an_unfinished_one(scout_dir: Path) -> None:
    tailer, radar = _tailer(scout_dir)
    assert tailer.poll([D]) == []  # no file yet
    append_pack(radar, pack(seq=0))
    append_pack(radar, '{"format":1,"kind":"pack","pack_id":"x"')  # Radar mid-write: no newline yet
    got = tailer.poll([D])
    assert [p["pack_id"] for p in got] == [f"{DAY}:7:0"]
    append_pack(radar, ',"symbol":"ABCD"}\n')  # the line is finished
    append_pack(radar, pack(seq=1))
    assert [p["pack_id"] for p in tailer.poll([D])] == ["x", f"{DAY}:7:1"]
    assert tailer.poll([D]) == []


def test_skips_bad_lines(scout_dir: Path) -> None:
    tailer, radar = _tailer(scout_dir)
    append_pack(radar, b"\xff\xfe not utf8\n")
    append_pack(radar, "not json\n")
    append_pack(radar, "[1, 2]\n")
    append_pack(radar, '{"format":2,"kind":"pack","pack_id":"a","symbol":"A"}\n')
    append_pack(radar, '{"format":1,"kind":"tape","pack_id":"b","symbol":"A"}\n')
    append_pack(radar, '{"format":1,"kind":"pack","symbol":"A"}\n')
    append_pack(radar, "\n")
    append_pack(radar, pack(seq=0))
    assert [p["pack_id"] for p in tailer.poll([D])] == [f"{DAY}:7:0"]


def test_restart_does_not_read_an_old_pack_again(scout_dir: Path) -> None:
    tailer, radar = _tailer(scout_dir)
    append_pack(radar, pack(seq=0))
    assert len(tailer.poll([D])) == 1
    again, _ = _tailer(scout_dir)  # a new Scout process
    assert again.poll([D]) == []
    append_pack(radar, pack(seq=1))
    assert [p["seq"] for p in again.poll([D])] == [1]


def test_a_shorter_file_is_read_again_from_the_start(scout_dir: Path) -> None:
    tailer, radar = _tailer(scout_dir)
    path = append_pack(radar, pack(seq=0))
    append_pack(radar, pack(seq=1))
    assert len(tailer.poll([D])) == 2
    path.write_bytes(b"")
    append_pack(radar, pack(seq=0, candidate_id=9))
    assert [p["candidate_id"] for p in tailer.poll([D])] == [9]  # the new content is not skipped
    append_pack(radar, pack(seq=2))
    assert [p["seq"] for p in tailer.poll([D])] == [2]


def test_spans_and_cursor_days(scout_dir: Path) -> None:
    tailer, radar = _tailer(scout_dir)
    path = append_pack(radar, pack(seq=0))
    append_pack(radar, "not json\n")
    append_pack(radar, pack(seq=1))
    spans = tailer.poll_spans([D])
    data = path.read_bytes()
    assert [(p.name, p.pack["seq"]) for p in spans] == [(path.name, 0), (path.name, 1)]
    assert spans[0].start == 0 and data[spans[0].end - 1:spans[0].end] == b"\n"
    assert spans[1].end == len(data) and data[spans[1].start:spans[1].end].startswith(b"{")
    assert tailer.cursor_days() == [D]


def test_no_radar_dir_means_no_packs(scout_dir: Path) -> None:
    cfg = make_config(scout_dir)
    assert packs_file(cfg, D) is None
    assert PackTailer(cfg, ScoutPaths.from_config(cfg)).poll([D]) == []


def test_packs_subdir_is_a_setting_inside_radar(scout_dir: Path) -> None:
    cfg = make_config(scout_dir, radar_dir="../Radar", radar_packs_subdir="research/packs")
    assert packs_file(cfg, D) == (scout_dir.parent / "Radar" / "research" / "packs" / f"{DAY}.packs.jsonl").resolve()
    from scout.config import ConfigError  # noqa: PLC0415

    for bad in ("..\\x", "C:\\x", "/abs", ""):
        with pytest.raises(ConfigError, match="radar_packs_subdir"):
            make_config(scout_dir, radar_dir="../Radar", radar_packs_subdir=bad)


def test_the_read_never_blocks_radar(scout_dir: Path) -> None:
    """While Scout holds the file open, Radar can still append to it, rename it and delete it."""
    _tailer_obj, radar = _tailer(scout_dir)
    path = append_pack(radar, pack(seq=0))
    with open_shared(path) as fh:
        with open(path, "ab") as writer:
            writer.write(b"more\n")
        moved = path.with_name("moved.jsonl")
        os.replace(path, moved)
        os.remove(moved)
        assert fh.read(10)
    assert not path.exists()


def test_tailer_never_writes_radar(scout_dir: Path, write_audit) -> None:  # noqa: ANN001
    tailer, radar = _tailer(scout_dir)
    append_pack(radar, pack(seq=0))
    with write_audit.watching(scout_dir / "state" / "reads"):
        assert len(tailer.poll([D])) == 1
