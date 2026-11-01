"""Add Levels (SCOUT_SPEC 5.2): validate, print every warning, ask once, copy with a sidecar; a
raising file is refused and nothing is copied; an added file is never edited or overwritten."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path

import pytest
from helpers_levels import FIXTURE_LIVE, plan_with, one_setup, read, write_read

from scout import levels_add as la
from scout.clock import ManualClock
from scout.models import LevelMeta
from scout.paths import ScoutPaths


class Asker:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self.answer


def _files(paths: ScoutPaths) -> list[str]:
    return sorted(p.name for p in paths.levels.iterdir())


def test_add_copies_byte_for_byte_with_a_sidecar(paths: ScoutPaths, clock: ManualClock, tmp_path: Path,
                                                 write_audit) -> None:
    src = write_read(tmp_path / "inbox", read(), "my read.json")
    lines: list[str] = []
    ask = Asker(True)
    with write_audit.watching(paths.root):
        result = la.add_file(paths, src, clock.now(), confirm=ask, out=lines.append)
    assert result.status == "added" and result.ok
    assert len(ask.prompts) == 1  # asked once
    assert _files(paths) == ["2026-09-15.tuesday.json", "2026-09-15.tuesday.meta.json"]
    assert (paths.levels / "2026-09-15.tuesday.json").read_bytes() == src.read_bytes()
    meta = LevelMeta.from_record(json.loads((paths.levels / "2026-09-15.tuesday.meta.json").read_text("utf-8")))
    assert meta.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    assert meta.original_filename == "my read.json" and meta.added_at == "2026-11-02T14:00:00Z"
    assert meta.session_date == "2026-09-15" and meta.source_kind == "tuesday" and meta.warnings == ()
    assert any("Setups: 4" in line for line in lines) and "Warnings: none" in lines


def test_every_warning_is_printed_and_stored(paths: ScoutPaths, clock: ManualClock) -> None:
    lines: list[str] = []
    result = la.add_file(paths, FIXTURE_LIVE, clock.now(), confirm=Asker(True), out=lines.append)
    assert result.status == "added"
    assert result.warnings and all(any(w in line for line in lines) for w in result.warnings)
    meta = json.loads((paths.levels / "2026-09-15.tuesday.meta.json").read_text("utf-8"))
    assert meta["warnings"] == list(result.warnings)
    assert meta["format"] == 1


@pytest.mark.parametrize("body", ["not json", json.dumps({"setups": []}), json.dumps({"session_date": "2026-09-15"})])
def test_a_raising_file_is_refused_and_nothing_is_copied(paths: ScoutPaths, clock: ManualClock, tmp_path: Path,
                                                         body: str) -> None:
    src = write_read(tmp_path, body, "bad.json")
    ask = Asker(True)
    result = la.add_file(paths, src, clock.now(), confirm=ask, out=lambda _s: None)
    assert result.status == "refused" and not result.ok
    assert ask.prompts == [] and _files(paths) == []


def test_declining_copies_nothing(paths: ScoutPaths, clock: ManualClock, tmp_path: Path) -> None:
    src = write_read(tmp_path, read())
    result = la.add_file(paths, src, clock.now(), confirm=Asker(False), out=lambda _s: None)
    assert result.status == "declined" and _files(paths) == []


def test_eof_at_the_question_is_no(paths: ScoutPaths, clock: ManualClock, tmp_path: Path) -> None:
    def eof(_prompt: str) -> bool:
        raise EOFError

    result = la.add_file(paths, write_read(tmp_path, read()), clock.now(), confirm=eof, out=lambda _s: None)
    assert result.status == "declined" and _files(paths) == []


def test_an_added_file_is_never_edited_or_overwritten(paths: ScoutPaths, clock: ManualClock,
                                                      tmp_path: Path) -> None:
    src = write_read(tmp_path / "a", read())
    assert la.add_file(paths, src, clock.now(), confirm=Asker(True), out=lambda _s: None).status == "added"
    target = paths.levels / "2026-09-15.tuesday.json"
    meta = paths.levels / "2026-09-15.tuesday.meta.json"
    before, meta_before = target.read_bytes(), meta.read_bytes()

    ask = Asker(True)
    again = la.add_file(paths, src, clock.now(), confirm=ask, out=lambda _s: None)
    assert again.status == "already_added" and again.ok and ask.prompts == []

    changed = write_read(tmp_path / "b", read(setups=[one_setup("ZZZ", [{"price": "1", "kind": "trigger"}])]))
    refused = la.add_file(paths, changed, clock.now(), confirm=ask, out=lambda _s: None)
    assert refused.status == "refused" and "never edits or replaces" in refused.message
    assert ask.prompts == []
    assert target.read_bytes() == before and meta.read_bytes() == meta_before
    assert _files(paths) == ["2026-09-15.tuesday.json", "2026-09-15.tuesday.meta.json"]


def test_kinds_and_dates_name_the_file(paths: ScoutPaths, clock: ManualClock, tmp_path: Path) -> None:
    la.add_file(paths, write_read(tmp_path, plan_with([], source_kind="daily", session_date="2026-11-03")),
                clock.now(), confirm=Asker(True), out=lambda _s: None)
    la.add_file(paths, write_read(tmp_path, plan_with([], source_kind="weekly", session_date="2026-11-03")),
                clock.now(), confirm=Asker(True), out=lambda _s: None)
    assert _files(paths) == ["2026-11-03.daily.json", "2026-11-03.daily.meta.json", "2026-11-03.weekly.json",
                             "2026-11-03.weekly.meta.json"]
    assert la.target_names("2026-11-03", "tuesday") == ("2026-11-03.tuesday.json", "2026-11-03.tuesday.meta.json")


def test_no_temp_file_is_left(paths: ScoutPaths, clock: ManualClock, tmp_path: Path) -> None:
    la.add_file(paths, write_read(tmp_path, read()), clock.now(), confirm=Asker(True), out=lambda _s: None)
    assert not [p for p in paths.levels.iterdir() if p.name.endswith(".tmp")]


# --------------------------------------------------------------------------- the command


class _Ctx:
    def __init__(self, paths: ScoutPaths, clock: ManualClock) -> None:
        self.paths, self.clock, self.log = paths, clock, logging.getLogger("test.levels_add")


def test_cli_adds_with_yes(paths: ScoutPaths, clock: ManualClock, tmp_path: Path, capsys) -> None:
    src = write_read(tmp_path, read())
    code = la.cli_levels_add(_Ctx(paths, clock), argparse.Namespace(file=str(src), yes=True))
    assert code == 0 and "Added" in capsys.readouterr().out
    assert (paths.levels / "2026-09-15.tuesday.json").exists()


def test_cli_takes_the_newest_inbox_file(paths: ScoutPaths, clock: ManualClock, monkeypatch) -> None:
    import os

    old = write_read(paths.inbox_jason, plan_with([], session_date="2026-09-01"), "old.json")
    new = write_read(paths.inbox_jason, plan_with([], session_date="2026-09-08"), "new.json")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert la.newest_inbox_file(paths) == new
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    code = la.cli_levels_add(_Ctx(paths, clock), argparse.Namespace(file=None, yes=False))
    assert code == 0 and (paths.levels / "2026-09-08.tuesday.json").exists()


def test_cli_refusal_and_no_file_exit_2(paths: ScoutPaths, clock: ManualClock, tmp_path: Path) -> None:
    ctx = _Ctx(paths, clock)
    assert la.cli_levels_add(ctx, argparse.Namespace(file=None, yes=True)) == 2
    bad = write_read(tmp_path, "nope", "bad.json")
    assert la.cli_levels_add(ctx, argparse.Namespace(file=str(bad), yes=True)) == 2
    assert _files(paths) == []


def test_cli_answer_no_adds_nothing(paths: ScoutPaths, clock: ManualClock, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    code = la.cli_levels_add(_Ctx(paths, clock), argparse.Namespace(file=str(write_read(tmp_path, read())), yes=False))
    assert code == 0 and _files(paths) == []


def test_main_dispatches_levels_add_and_score(scout_dir: Path, tmp_path: Path, capsys) -> None:
    from scout import cli

    config = scout_dir / "config.toml"
    config.write_text('contact_email = ""\n', encoding="utf-8")
    src = write_read(tmp_path, read())
    assert cli.main(["--config", str(config), "levels", "add", str(src), "--yes"]) == 0
    assert (scout_dir / "state" / "levels" / "2026-09-15.tuesday.json").read_bytes() == src.read_bytes()
    assert cli.main(["--config", str(config), "levels", "score"]) == 2  # radar_dir is empty
    assert "radar_dir is empty" in capsys.readouterr().out
