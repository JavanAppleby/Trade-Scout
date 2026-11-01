"""Read-only views of Radar's and Pilot's files (SCOUT_SPEC 2.4, DESIGN 2.1)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scout.clock import ManualClock
from scout.config import config_from_dict
from scout.inputs import covered_symbols, pilot_positions, radar_finds


def test_missing_inputs_are_empty(cfg, clock: ManualClock) -> None:  # noqa: ANN001
    assert radar_finds(cfg, date(2026, 11, 2)) == [] and pilot_positions(cfg, clock.now()) == []
    assert covered_symbols(cfg, clock.now()) == []


def test_reads_finds_positions_and_watch_list(tmp_path: Path, scout_dir: Path, clock: ManualClock,
                                              write_audit) -> None:  # noqa: ANN001
    radar, pilot = tmp_path / "Radar", tmp_path / "Pilot"
    (radar / "state" / "finds").mkdir(parents=True)
    (radar / "state" / "finds" / "2026-11-02.jsonl").write_text(
        '{"candidate_id": "c1", "symbol": "abcd", "found_at": "2026-11-02T13:00:00Z", "price": 3.2}\n'
        '{"symbol": ""}\nnot json\n{"candidate_id": "c2", "symbol": "EFGH", "price": "x"}\n', encoding="utf-8")
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
