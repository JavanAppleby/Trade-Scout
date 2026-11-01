"""scout_reader.records: tolerant reading, the as-of rule, params and the governing beat (DESIGN 3.2-3.3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from scout_reader import records as R

from conftest import START
from helpers_fakes import write_records

Z = "2026-11-02T14:00:00Z"


def _t(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_visible_as_of_rule() -> None:
    assert R.visible({"first_seen_at": Z}, START)
    assert not R.visible({"first_seen_at": _t(1)}, START)
    assert not R.visible({"first_seen_at": Z, "published_at": _t(1)}, START)
    assert R.visible({"first_seen_at": Z, "published_at": None}, START)
    assert not R.visible({"first_seen_at": Z, "published_at": "garbage"}, START)
    assert R.visible({"labelled_at": Z}, START) and not R.visible({"labelled_at": _t(5)}, START)
    assert not R.visible({}, START) and not R.visible({"first_seen_at": "2026-11-02T14:00:00"}, START)


def test_known_formats() -> None:
    assert R.known({"format": 1})
    assert not R.known({}) and not R.known({"format": 2}) and not R.known({"format": True})
    assert not R.known({"format": "1"})


def test_day_files_by_et_date(tmp_path: Path) -> None:
    files = R.day_files(tmp_path, datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc),  # 23:00 ET on 1 Nov
                        datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc))  # 01:00 ET on 3 Nov
    assert [f.name for f in files] == ["2026-11-01.jsonl", "2026-11-02.jsonl", "2026-11-03.jsonl"]


def test_last_params(scout_dir: Path) -> None:
    write_records(scout_dir, "params", [{"first_seen_at": _t(-100), "scout_stale_s": 120},
                                        {"first_seen_at": _t(50), "scout_stale_s": 60},
                                        {"format": 9, "first_seen_at": _t(-10), "scout_stale_s": 1}])
    assert R.last_params(scout_dir, START)["scout_stale_s"] == 120
    assert R.last_params(scout_dir, START + timedelta(seconds=60))["scout_stale_s"] == 60
    assert R.last_params(scout_dir, START - timedelta(seconds=200)) is None


def test_governing_beat(scout_dir: Path) -> None:
    write_records(scout_dir, "beats", [{"at": _t(-200), "stopped": False, "sources": {}, "gap_open": {}},
                                       {"at": _t(-30), "stopped": False, "sources": {"x": "ok"}, "gap_open": {}}],
                  raw_lines=['{"format":1,"at":"' + _t(-1) + '","stopped":false'])  # a partial last line
    beat = R.governing_beat(scout_dir, START)
    assert beat is not None and beat["at"] == _t(-30)
    assert R.governing_beat(scout_dir, START + timedelta(seconds=100)) is None  # older than 120 s: stale
    assert R.governing_beat(scout_dir, START, stale_s=10) is None
    assert R.governing_beat(scout_dir, START - timedelta(seconds=31)) is None  # future beats are invisible


def test_stopped_beat_governs(scout_dir: Path) -> None:
    write_records(scout_dir, "beats", [{"at": _t(-30), "stopped": False, "sources": {}, "gap_open": {}},
                                       {"at": _t(-10), "stopped": True, "sources": {}, "gap_open": {}}])
    assert R.governing_beat(scout_dir, START) is None


def test_corrupt_files_read_as_missing(scout_dir: Path) -> None:
    beats = scout_dir / "state" / "beats" / "2026-11-02.jsonl"
    beats.write_bytes(b"\x00\x01garbage\n{not json}\n")
    assert R.governing_beat(scout_dir, START) is None
    assert R.read_json(beats) is None and R.read_json(scout_dir / "nope.json") is None


def test_result_shapes() -> None:
    assert R.result("true", ["f1"]) == {"value": "true", "evidence": ["f1"], "stale": False, "reasons": []}
    assert R.result("maybe")["value"] == "unknown"
    assert R.UNKNOWN("scout_stale", stale=True) == {"value": "unknown", "evidence": [], "stale": True,
                                                    "reasons": ["scout_stale"]}


def test_a_deeply_nested_line_is_skipped_not_raised(tmp_path) -> None:
    """Regression: read_jsonl caught only UnicodeDecodeError and ValueError, so one line of 200000
    '[' raised RecursionError into every caller (and made every answer unknown for the day)."""
    from scout_reader import records as R

    NL = bytes([10])
    path = tmp_path / "x.jsonl"
    path.write_bytes(b'{"a":1}' + NL + b"[" * 200000 + NL)
    assert R.read_jsonl(path) == [{"a": 1}]
    one = tmp_path / "y.json"
    one.write_bytes(b"[" * 200000)
    assert R.read_json(one) is None
