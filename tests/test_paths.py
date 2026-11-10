"""Scout's folder and the write gate (SCOUT_SPEC 2.4, DESIGN 4)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from scout.config import config_from_dict
from scout.paths import OwnFolderError, ScoutPaths, _plain, is_within, overlaps


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


def test_the_long_path_prefix_is_dropped_for_drive_and_unc_forms_only() -> None:
    assert _plain(r"\\?\C:\Scout\state\x.json") == r"C:\Scout\state\x.json"
    assert _plain(r"\\?\UNC\server\share\x") == r"\\server\share\x"
    for other in (r"\\?\Volume{1b3b1146-4076-11e1-84aa-806e6f6e6963}\x", r"\\?\GLOBALROOT\Device\x", r"\\?\C:",
                  r"C:\Scout\x", r"\\server\share\x", "state/x"):
        assert _plain(other) == other
    assert _plain(Path("state") / "x") == str(Path("state") / "x")


@pytest.mark.skipif(os.name != "nt", reason="the long-path form is Windows'")
def test_writable_judges_a_long_form_path_like_the_plain_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r"""`Path.resolve()` can return `\\?\C:\...` for an ordinary path (it keeps the prefix when another
    thread makes the folder while it resolves). The gate allows it inside Scout's folder, returns
    the plain path, and still refuses everything it refused before."""
    scout, radar = tmp_path / "Scout", tmp_path / "Radar"
    scout.mkdir()
    radar.mkdir()
    p = ScoutPaths(scout, others=(radar,))
    want = (scout / "state" / "reads" / "_tail.json").resolve()
    real = Path.resolve

    def long_form(self: Path, strict: bool = False) -> Path:
        out = str(real(self, strict=strict))
        return Path(out if out.startswith("\\\\") else "\\\\?\\" + out)

    monkeypatch.setattr(Path, "resolve", long_form)
    got = p.writable("state/reads/_tail.json")
    assert got == want and not str(got).startswith("\\\\?\\")
    assert is_within(Path("\\\\?\\" + str(want)), scout) and not is_within(Path("\\\\?\\" + str(radar)), scout)
    for bad in (tmp_path / "x", scout / ".." / "x", radar / "state" / "x", Path("../Radar/x")):
        with pytest.raises(OwnFolderError):
            p.writable(bad)


@pytest.mark.skipif(os.name != "nt", reason="the long-path form is Windows'")
def test_writable_never_refuses_a_path_while_another_thread_makes_its_folder(tmp_path: Path) -> None:
    """0.3.0: the reads workers write beside the loop. A worker making `state/reads/...` while the
    loop asks the gate about a file in it must never turn that file into a refused write."""
    p = ScoutPaths(tmp_path / "Scout")
    p.root.mkdir()
    refused: list[str] = []
    for i in range(300):
        folder = p.root / "state" / f"r{i}" / "ledger"
        go = threading.Event()
        maker = threading.Thread(target=lambda f=folder: (go.wait(), f.mkdir(parents=True, exist_ok=True)))
        maker.start()
        go.set()
        for _ in range(4):
            try:
                p.writable(folder.parent / "_tail.json")
            except OwnFolderError as exc:  # pragma: no cover - the defect
                refused.append(str(exc))
        maker.join()
    assert refused == []


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
