"""Read-only views of Radar's and Pilot's files (SCOUT_SPEC 2.4, DESIGN 2.1)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scout.clock import ManualClock
from scout.config import config_from_dict
from scout.config_load import ConfigError
from scout.inputs import covered_symbols, pilot_positions, radar_finds


def test_missing_inputs_are_empty(cfg, clock: ManualClock) -> None:  # noqa: ANN001
    assert radar_finds(cfg, date(2026, 11, 2)) == [] and pilot_positions(cfg, clock.now()) == []
    assert covered_symbols(cfg, clock.now()) == []


def test_reads_finds_positions_and_watch_list(tmp_path: Path, scout_dir: Path, clock: ManualClock,
                                              write_audit) -> None:  # noqa: ANN001
    radar, pilot = tmp_path / "Radar", tmp_path / "Pilot"
    (radar / "state" / "handoff").mkdir(parents=True)
    (radar / "state" / "handoff" / "2026-11-02.jsonl").write_text(
        '{"format": 1, "candidate_id": 1, "trading_day": "2026-11-02", "symbol": "abcd", '
        '"found_at": "2026-11-02T13:00:00Z", "price": "3.2000"}\n'
        '{"format": 1, "symbol": ""}\nnot json\n{"format": 1, "candidate_id": 2, "symbol": "EFGH", "price": "x"}\n',
        encoding="utf-8")
    (pilot / "state" / "plans").mkdir(parents=True)
    (pilot / "state" / "plans" / "a.json").write_text(json.dumps({"symbol": "WXYZ", "status": "open"}), "utf-8")
    (pilot / "state" / "plans" / "b.json").write_text(json.dumps({"symbol": "OLD", "status": "closed"}), "utf-8")
    (pilot / "state" / "plans" / "c.json").write_text("{broken", "utf-8")
    cfg = config_from_dict({"radar_dir": str(radar), "pilot_dir": str(pilot), "watch_symbols": ["QRST"]}, scout_dir)
    with write_audit.watching(scout_dir):  # reading writes nothing anywhere
        finds = radar_finds(cfg, date(2026, 11, 2))
        positions = pilot_positions(cfg, clock.now())
        covered = covered_symbols(cfg, clock.now())
    assert write_audit.events == []
    assert [(f.symbol, f.price) for f in finds] == [("ABCD", 3.2), ("EFGH", None)]
    assert positions == ["WXYZ"]
    assert covered == ["ABCD", "EFGH", "QRST", "WXYZ"]


def _handoff(radar: Path, day: str, *lines: dict) -> None:
    folder = radar / "state" / "handoff"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{day}.jsonl", "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(line) + "\n" for line in lines))


def test_reads_radars_real_handoff_line(tmp_path: Path, scout_dir: Path) -> None:
    """A line as Radar 1.0.3 wrote it on 2026-11-03 (state/handoff): money as strings."""
    radar = tmp_path / "Radar"
    _handoff(radar, "2026-11-03", {
        "format": 1, "candidate_id": 69, "trading_day": "2026-11-03", "session": "PRE", "symbol": "PERI",
        "found_at": "2026-11-03T09:00:03.615894Z", "price": "11.3915", "bid": "10.9500", "ask": "11.5700",
        "spread_pct": "0.055062", "prev_close": "8.700000", "change_pct": "0.309368", "volume": "7816096",
        "rel_volume_10d": "5.2522", "tags": [], "flags": [], "radar_release": "radar 1.0.3",
        "radar_config_hash": "6d2e008da2fa"})
    cfg = config_from_dict({"radar_dir": str(radar)}, scout_dir)
    finds = radar_finds(cfg, date(2026, 11, 3))
    assert [(f.candidate_id, f.symbol, f.found_at, f.price) for f in finds] == [
        ("69", "PERI", "2026-11-03T09:00:03.615894Z", 11.3915)]


def test_other_formats_and_days_are_skipped(tmp_path: Path, scout_dir: Path) -> None:
    radar = tmp_path / "Radar"
    _handoff(radar, "2026-11-03",
             {"format": 2, "symbol": "NEWF", "trading_day": "2026-11-03"},
             {"symbol": "NOFMT", "trading_day": "2026-11-03"},
             {"format": True, "symbol": "BOOL", "trading_day": "2026-11-03"},
             {"format": 1, "symbol": "OTHER", "trading_day": "2026-11-02"},
             {"format": 1, "symbol": "GOOD", "trading_day": "2026-11-03", "price": "NaN"})
    cfg = config_from_dict({"radar_dir": str(radar)}, scout_dir)
    assert [(f.symbol, f.price) for f in radar_finds(cfg, date(2026, 11, 3))] == [("GOOD", None)]


def test_the_handoff_sub_folder_is_a_config_key(tmp_path: Path, scout_dir: Path) -> None:
    radar = tmp_path / "Radar"
    folder = radar / "out"
    folder.mkdir(parents=True)
    (folder / "2026-11-03.jsonl").write_text(
        json.dumps({"format": 1, "symbol": "MOVD", "trading_day": "2026-11-03"}) + "\n", "utf-8")
    cfg = config_from_dict({"radar_dir": str(radar), "radar_handoff_subdir": "out"}, scout_dir)
    assert [f.symbol for f in radar_finds(cfg, date(2026, 11, 3))] == ["MOVD"]
    for bad in ("", "../elsewhere", "C:/abs"):
        with pytest.raises(ConfigError):
            config_from_dict({"radar_dir": str(radar), "radar_handoff_subdir": bad}, scout_dir)


def test_covered_symbols_after_20_et_also_read_the_next_trading_days_file(tmp_path: Path, scout_dir: Path,
                                                                           et) -> None:  # noqa: ANN001
    radar = tmp_path / "Radar"
    _handoff(radar, "2026-11-03", {"format": 1, "symbol": "TUES", "trading_day": "2026-11-03"})
    _handoff(radar, "2026-11-04", {"format": 1, "symbol": "LATE", "trading_day": "2026-11-04"})
    cfg = config_from_dict({"radar_dir": str(radar)}, scout_dir)
    assert covered_symbols(cfg, et("2026-11-03", "19:45")) == ["TUES"]
    assert covered_symbols(cfg, et("2026-11-03", "20:30")) == ["LATE", "TUES"]
