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


# --------------------------------------------------------------------------- auto-add (owner decision 2026-11-01)

INBOX_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scout_inbox" / "jason"


def _inbox_copy(paths: ScoutPaths, *names: str) -> list[Path]:
    paths.inbox_jason.mkdir(parents=True, exist_ok=True)
    out = []
    for name in names:
        target = paths.inbox_jason / name
        target.write_bytes((INBOX_FIXTURES / name).read_bytes())
        out.append(target)
    return out


def test_the_daily_and_weekly_inbox_files_load_as_decided(paths: ScoutPaths) -> None:
    from scout.levels import load_folder, load_plan

    daily = load_plan(INBOX_FIXTURES / "2026-11-02.daily.json")
    assert daily["source_kind"] == "daily" and daily["warnings"] == []
    acme = daily["setups"][0]
    assert acme["expires"] == "2026-11-02" and acme["direction"] == "neutral"
    kinds = [(lv["kind"], str(lv["price"])) for lv in acme["levels"]]
    assert kinds == [("trigger", "10.50"), ("target", "10.90"), ("target", "11.20"), ("invalidation", "10.10"),
                     ("support", "9.80"), ("support", "9.50")]
    assert acme["levels"][0]["note"] == "long trigger (card: 'long > 10.50')"
    weekly = load_plan(INBOX_FIXTURES / "2026-11-02.weekly.json")
    assert weekly["source_kind"] == "weekly" and weekly["warnings"] == []
    assert {s["expires"] for s in weekly["setups"]} == {"2026-11-06"}
    assert weekly["setups"][1]["direction"] == "long" and weekly["setups"][1]["conditionals"]
    # merged: on its day the daily read comes first for OMEG; the next day only the weekly one is current
    for name in ("2026-11-02.daily.json", "2026-11-02.weekly.json"):
        (paths.levels / name).write_bytes((INBOX_FIXTURES / name).read_bytes())
    monday = load_folder(paths.levels, "2026-11-02")
    assert [s["source_kind"] for s in monday["setups"] if s["symbol"] == "OMEG"] == ["daily", "weekly"]
    tuesday = load_folder(paths.levels, "2026-11-03")
    assert {s["symbol"] for s in tuesday["setups"]} == {"SPY", "OMEG"}
    assert tuesday["files"] == ["2026-11-02.weekly.json"]


def test_cli_yes_refusal_is_one_line_non_zero_and_leaves_a_note(paths: ScoutPaths, clock: ManualClock,
                                                                 capsys) -> None:
    src = write_read(paths.inbox_jason, plan_with([], source_kind="daily", session_date="2026-11-02"),
                     "2026-11-02.daily.json")
    code = la.cli_levels_add(_Ctx(paths, clock), argparse.Namespace(file=str(src), yes=True, inbox=False))
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 2
    assert out[-1] == "REFUSED: 2026-11-02.daily.json: Refused: no setup survived validation. Nothing was copied."
    note = paths.inbox_jason / "2026-11-02.daily.json.rejected.txt"
    text = note.read_text(encoding="utf-8")
    assert text.startswith("REFUSED: Refused: no setup survived validation.")
    assert f"sha256: {hashlib.sha256(src.read_bytes()).hexdigest()}" in text
    assert _files(paths) == []


@pytest.mark.parametrize("data, name, needle", [
    (read(source_kind=None), "2026-09-15.tuesday.json", "source_kind must be one of"),
    (read(source_kind="monthly"), "2026-09-15.tuesday.json", "source_kind must be one of"),
    (read(), "2026-09-16.tuesday.json", "disagrees with its session_date"),
    (read(), "2026-09-15.daily.json", "disagrees with its session_date"),
])
def test_auto_mode_refuses_what_a_person_would_catch(paths: ScoutPaths, clock: ManualClock, data: dict, name: str,
                                                     needle: str) -> None:
    src = write_read(paths.inbox_jason, data, name)
    result = la.add_file(paths, src, clock.now(), confirm=Asker(True), out=lambda _s: None, auto=True)
    assert result.status == "refused" and needle in result.message and _files(paths) == []
    if needle.startswith("source_kind"):  # the owner's own Add Levels (not auto) still takes it, with a warning
        assert la.add_file(paths, src, clock.now(), confirm=Asker(True), out=lambda _s: None).status == "added"


def test_inbox_mode_adds_each_new_file_once(paths: ScoutPaths, clock: ManualClock, capsys) -> None:
    _inbox_copy(paths, "2026-11-02.daily.json", "2026-11-02.weekly.json")
    bad = write_read(paths.inbox_jason, read(), "jason read.json")  # not named <date>.<kind>.json
    ctx = _Ctx(paths, clock)
    args = argparse.Namespace(file=None, yes=False, inbox=True)
    assert la.cli_levels_add(ctx, args) == 2
    out = capsys.readouterr().out
    assert "Inbox: 2 added, 1 refused, 0 already added, 0 skipped" in out
    assert "REFUSED: jason read.json: Refused: the file name must be <session_date>.<source_kind>.json" in out
    assert _files(paths) == ["2026-11-02.daily.json", "2026-11-02.daily.meta.json", "2026-11-02.weekly.json",
                             "2026-11-02.weekly.meta.json"]
    assert (paths.inbox_jason / "jason read.json.rejected.txt").exists()
    # idempotent: nothing new, nothing refused again
    assert la.cli_levels_add(ctx, args) == 0
    assert "Inbox: 0 added, 0 refused, 0 already added, 3 skipped" in capsys.readouterr().out
    # new bytes under the refused name are tried again (still the wrong name, so refused again)
    bad.write_text(json.dumps(read(market="QQQ")), encoding="utf-8")
    assert la.cli_levels_add(ctx, args) == 2
    assert la.cli_levels_add(ctx, argparse.Namespace(file="x.json", yes=False, inbox=True)) == 2


def test_inbox_mode_ignores_temp_and_hidden_files(paths: ScoutPaths, clock: ManualClock) -> None:
    _inbox_copy(paths, "2026-11-02.daily.json")
    (paths.inbox_jason / "2026-11-03.daily.json.tmp").write_text("{", encoding="utf-8")
    (paths.inbox_jason / ".2026-11-03.daily.json").write_text("{", encoding="utf-8")
    results, skipped = la.add_inbox(paths, clock.now(), out=lambda _s: None)
    assert [r.status for r in results] == ["added"] and skipped == 0


def test_a_lost_sidecar_is_written_on_the_next_add(paths: ScoutPaths, clock: ManualClock) -> None:
    (src,) = _inbox_copy(paths, "2026-11-02.daily.json")
    assert la.add_file(paths, src, clock.now(), confirm=Asker(True), out=lambda _s: None, auto=True).status == "added"
    meta = paths.levels / "2026-11-02.daily.meta.json"
    meta.unlink()
    again = la.add_file(paths, src, clock.now(), confirm=Asker(True), out=lambda _s: None, auto=True)
    assert again.status == "already_added" and meta.exists()
    assert hashlib.sha256(src.read_bytes()).hexdigest() in la.added_digests(paths)


def test_a_race_for_one_name_never_replaces(paths: ScoutPaths, clock: ManualClock, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    mine = write_read(tmp_path / "a", read())
    theirs = write_read(tmp_path / "b", read(setups=[one_setup("ZZZ", [{"price": "1", "kind": "trigger"}])]))
    real = la._write_new

    def other_add_first(p: ScoutPaths, target: Path, body: bytes) -> None:
        target.write_bytes(theirs.read_bytes())  # another `levels add` lands between the check and the link
        real(p, target, body)

    monkeypatch.setattr(la, "_write_new", other_add_first)
    result = la.add_file(paths, mine, clock.now(), confirm=Asker(True), out=lambda _s: None)
    assert result.status == "refused" and "never edits or replaces" in result.message
    assert (paths.levels / "2026-09-15.tuesday.json").read_bytes() == theirs.read_bytes()


def test_the_snapshot_never_sees_a_temp_file(paths: ScoutPaths, clock: ManualClock) -> None:
    from scout.levels import level_files

    (src,) = _inbox_copy(paths, "2026-11-02.daily.json")
    (paths.levels / ".2026-11-02.daily.json.99.tmp").write_text("{half", encoding="utf-8")
    la.add_file(paths, src, clock.now(), confirm=Asker(True), out=lambda _s: None, auto=True)
    assert [p.name for p in level_files(paths.levels)] == ["2026-11-02.daily.json"]


def test_a_transient_read_error_leaves_no_note_and_the_next_inbox_adds_it(paths: ScoutPaths, clock: ManualClock,
                                                                         monkeypatch, capsys) -> None:
    """A scanner holding the file (PermissionError) is not a verdict: no note, so `--inbox` retries it."""
    (src,) = _inbox_copy(paths, "2026-11-02.daily.json")
    real = Path.read_bytes
    calls: list[Path] = []

    def locked_once(self: Path) -> bytes:
        if self == src and not calls:
            calls.append(self)
            raise PermissionError("invented sharing violation")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", locked_once)
    ctx = _Ctx(paths, clock)
    assert la.cli_levels_add(ctx, argparse.Namespace(file=str(src), yes=True, inbox=False)) == 2
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("REFUSED: 2026-11-02.daily.json: Refused: ")
    assert not la.rejected_note(src).exists()
    assert la.cli_levels_add(ctx, argparse.Namespace(file=None, yes=False, inbox=True)) == 0
    assert "Inbox: 1 added, 0 refused" in capsys.readouterr().out
    assert (paths.levels / "2026-11-02.daily.json").read_bytes() == src.read_bytes()


def test_a_note_names_the_bytes_checked_so_a_finished_write_is_retried(paths: ScoutPaths, clock: ManualClock,
                                                                       capsys) -> None:
    """A reader that catches the file mid-write refuses the partial bytes; the note carries their
    sha256, so the complete file (different bytes) is tried again and added."""
    full = (INBOX_FIXTURES / "2026-11-02.daily.json").read_bytes()
    paths.inbox_jason.mkdir(parents=True, exist_ok=True)
    src = paths.inbox_jason / "2026-11-02.daily.json"
    src.write_bytes(full[: len(full) // 2])
    ctx = _Ctx(paths, clock)
    args = argparse.Namespace(file=None, yes=False, inbox=True)
    assert la.cli_levels_add(ctx, args) == 2
    note = la.rejected_note(src).read_text(encoding="utf-8")
    assert f"sha256: {hashlib.sha256(full[: len(full) // 2]).hexdigest()}" in note
    src.write_bytes(full)
    capsys.readouterr()
    assert la.cli_levels_add(ctx, args) == 0
    assert "Inbox: 1 added, 0 refused" in capsys.readouterr().out


def test_the_bytes_copied_are_the_bytes_checked(paths: ScoutPaths, clock: ManualClock, monkeypatch) -> None:
    (src,) = _inbox_copy(paths, "2026-11-02.daily.json")
    real = Path.read_bytes
    reads: list[Path] = []

    def counting(self: Path) -> bytes:
        if self == src:
            reads.append(self)
        return real(self)

    real_text = Path.read_text

    def counting_text(self: Path, *a, **k) -> str:
        if self == src:
            reads.append(self)
        return real_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", counting)
    monkeypatch.setattr(Path, "read_text", counting_text)
    result = la.add_file(paths, src, clock.now(), confirm=Asker(True), out=lambda _s: None, auto=True)
    assert result.status == "added" and len(reads) == 1
    assert result.digest == hashlib.sha256(src.read_bytes()).hexdigest()
