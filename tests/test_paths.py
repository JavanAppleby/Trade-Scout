"""Scout's folder and the write gate (SCOUT_SPEC 2.4, DESIGN 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scout.config import config_from_dict
from scout.paths import OwnFolderError, ScoutPaths, is_within, overlaps


def test_layout(paths: ScoutPaths, scout_dir: Path) -> None:
    assert paths.root == scout_dir
    assert paths.facts == scout_dir / "state" / "facts"
    assert paths.edgar_due == scout_dir / "state" / "edgar" / "due"
    assert paths.heartbeat_file == scout_dir / "state" / "scout.heartbeat"
    assert paths.params_file == scout_dir / "state" / "params.jsonl"
    assert paths.levels == scout_dir / "state" / "levels"
    assert paths.inbox_jason == scout_dir / "inbox" / "jason"
    assert paths.env_file == scout_dir / ".env"


def test_ensure_creates_only_inside(tmp_path: Path, write_audit) -> None:  # noqa: ANN001
    root = tmp_path / "S"
    root.mkdir()
    with write_audit.watching(root):
        ScoutPaths(root).ensure()
    assert (root / "state" / "beats").is_dir() and (root / "inbox" / "jason").is_dir()
    assert write_audit.violations == []


def test_writable_refuses_outside_and_other_bots(tmp_path: Path) -> None:
    scout, radar = tmp_path / "Scout", tmp_path / "Radar"
    scout.mkdir()
    radar.mkdir()
    p = ScoutPaths(scout, others=(radar,))
    assert p.writable("state/x.json") == (scout / "state" / "x.json").resolve()
    for bad in (tmp_path / "x", scout / ".." / "x", radar / "state" / "x", Path("../Radar/x")):
        with pytest.raises(OwnFolderError):
            p.writable(bad)


def test_overlapping_folders_refused(tmp_path: Path) -> None:
    with pytest.raises(OwnFolderError):
        ScoutPaths(tmp_path / "S", others=(tmp_path,))
    with pytest.raises(OwnFolderError):
        ScoutPaths(tmp_path / "S", levels_dir="../L")


def test_from_config_carries_other_bots(scout_dir: Path, tmp_path: Path) -> None:
    cfg = config_from_dict({"radar_dir": str(tmp_path / "Radar"), "pilot_dir": "../Pilot"}, scout_dir)
    p = ScoutPaths.from_config(cfg)
    assert set(p.others) == {(tmp_path / "Radar").resolve(), (tmp_path / "Pilot").resolve()}


def test_is_within_and_overlaps(tmp_path: Path) -> None:
    assert is_within(tmp_path / "a" / "b", tmp_path / "a")
    assert not is_within(tmp_path / "ab", tmp_path / "a")
    assert overlaps(tmp_path, tmp_path / "a") and not overlaps(tmp_path / "a", tmp_path / "b")
