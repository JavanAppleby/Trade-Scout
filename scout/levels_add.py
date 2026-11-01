"""Add Levels: validate the owner's level file and file it, never editing it (SCOUT_SPEC 5.2, DESIGN 2.4).

`python -m scout levels add <file> [--yes]` (the **Add Levels** launcher):
1. read the file once and validate those bytes with the loader (`scout.levels.plan_from_data`); a
   file that raises is refused and nothing is copied;
2. print a summary and every warning;
3. ask once (`--yes` skips the question);
4. copy the file byte-for-byte to `state\\levels\\<session_date>.<source_kind>.json` and write the
   sidecar `<session_date>.<source_kind>.meta.json` (`added_at`, `sha256`, `original_filename`,
   `session_date`, `source_kind`, `warnings`).

An added file is never edited or overwritten: the same bytes again is "already added" (nothing
written, except a missing sidecar, which is written); different bytes for the same session date and
kind are refused.

Auto-add (owner decision 2026-11-01; docs/INBOX.md): the scheduled Claude tasks run
`levels add --yes <file>` or `levels add --inbox`. Both are "auto" mode, which refuses more: a file
with no valid `source_kind`, a file with no setup left after validation, and a file named like a read
(`<date>.<kind>.json`) whose name disagrees with its own `session_date` and `source_kind` (`--inbox`
also refuses any other name). A refusal prints a last line starting "REFUSED:", exits 2, and leaves
`<file>.rejected.txt` next to the file when that file is inside Scout's folder (the reason, the time
and the sha256 of the bytes that were checked). A file that could not be read (missing, locked by a
scanner, or still being written) is refused without a note, so the next `--inbox` reads it again.
The file is read once: the bytes validated are the bytes hashed and copied. `--inbox` adds every `*.json` in `inbox\\jason` whose sha256 is not already
added and not already refused in that form, so a task can call it after every write: it is
idempotent.

Safe while Scout runs: this command takes no lock and writes only `state\\levels` and the inbox notes.
The level file appears in one step (a temp file hard-linked, or on a file system without links
renamed, to a name that must not exist), so a reader never sees half a file and two adds racing for
one name cannot both win. The snapshot job skips temp files and sidecars and re-reads the folder when
its file set changes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from scout.clock import iso
from scout.levels import META_SUFFIX, JasonPlanError, plan_from_data
from scout.levels_parse import SOURCE_KINDS
from scout.models import LevelMeta
from scout.paths import OwnFolderError, ScoutPaths
from scout.store import atomic_write_json, atomic_write_text, sha256_bytes

__all__ = ["REJECTED_SUFFIX", "AddResult", "add_file", "add_inbox", "added_digests", "cli_levels_add",
           "inbox_files", "newest_inbox_file", "rejected_note", "summary_lines", "target_names"]

ADDED, ALREADY, REFUSED, DECLINED = "added", "already_added", "refused", "declined"
REJECTED_SUFFIX = ".rejected.txt"
_READ_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.([a-z]+)\.json$")

Confirm = Callable[[str], bool]
Out = Callable[[str], Any]


@dataclass(frozen=True)
class AddResult:
    status: str  # added | already_added | refused | declined
    message: str
    target: Path | None = None
    meta: Path | None = None
    warnings: tuple[str, ...] = field(default=())
    #: sha256 of the exact bytes that were checked (None when the file could not be read).
    digest: str | None = None
    #: True when the file could not be read (an I/O error): no refusal note, so the next try reads it.
    io_error: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (ADDED, ALREADY)


def target_names(session_date: str, source_kind: str) -> tuple[str, str]:
    """(level file name, sidecar name) for a read."""
    stem = f"{session_date}.{source_kind}"
    return f"{stem}.json", f"{stem}.meta.json"


def summary_lines(plan: dict) -> list[str]:
    """The plain-English summary printed before the question."""
    setups = plan["setups"]
    n_levels = sum(len(s["levels"]) for s in setups)
    expiries = sorted({s["expires"] for s in setups})
    lines = [f"Session date: {plan['session_date']}   kind: {plan['source_kind']}",
             f"Setups: {len(setups)}   levels watched (every reading counted): {n_levels}"]
    if expiries:
        lines.append(f"Expires: {expiries[0]}" + (f" to {expiries[-1]}" if expiries[-1] != expiries[0] else ""))
    ambiguous = sum(1 for s in setups for lvl in s["levels"] if lvl["ambiguous"])
    if ambiguous:
        lines.append(f"Ambiguous level readings: {ambiguous} (each reading is kept and says so)")
    return lines


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def _auto_problem(plan: dict, body: bytes, src: Path, inbox_only: bool) -> str | None:
    """Why auto mode refuses a file the loader accepted (None when it does not)."""
    try:
        raw_kind = json.loads(body.decode("utf-8-sig")).get("source_kind")
    except (ValueError, AttributeError):  # pragma: no cover - the loader read it already
        raw_kind = None
    if not isinstance(raw_kind, str) or raw_kind.strip().lower() not in SOURCE_KINDS:
        return f"source_kind must be one of {', '.join(SOURCE_KINDS)} in an auto-added file"
    if not plan["setups"]:
        return "no setup survived validation"
    m = _READ_NAME.match(src.name)
    if m is None:
        return f"the file name must be <session_date>.<source_kind>.json, got {src.name!r}" if inbox_only else None
    if (m.group(1), m.group(2)) != (plan["session_date"], plan["source_kind"]):
        return (f"the file name {src.name!r} disagrees with its session_date {plan['session_date']} and source_kind "
                f"{plan['source_kind']}")
    return None


def add_file(paths: ScoutPaths, src: Path | str, now: datetime, *, confirm: Confirm,
             out: Out = print, auto: bool = False, inbox_only: bool = False) -> AddResult:
    """Validate `src`, show its warnings, ask once, then copy it and its sidecar into `paths.levels`.
    With `auto` (the scheduled tasks' `--yes` and `--inbox`) the stricter checks of the module doc apply."""
    src = Path(src)
    try:  # one read: the bytes checked are the bytes hashed and copied
        body = src.read_bytes()
    except OSError as exc:  # missing, locked or mid-write: not a verdict on the file
        message = f"Refused: {src.name} could not be read ({type(exc).__name__}). Nothing was copied."
        out(message)
        return AddResult(REFUSED, message, io_error=True)
    digest = sha256_bytes(body)
    try:
        plan = _plan_from_bytes(body, src.name)
    except JasonPlanError as exc:
        message = f"Refused: {_one_line(exc)}. Nothing was copied."
        out(message)
        return AddResult(REFUSED, message, digest=digest)

    warnings = tuple(plan["warnings"])
    if auto:
        problem = _auto_problem(plan, body, src, inbox_only)
        if problem:
            message = f"Refused: {problem}. Nothing was copied."
            out(message)
            return AddResult(REFUSED, message, warnings=warnings, digest=digest)

    for line in summary_lines(plan):
        out(line)
    if warnings:
        out(f"Warnings ({len(warnings)}):")
        for w in warnings:
            out(f"  - {w}")
    else:
        out("Warnings: none")

    name, meta_name = target_names(plan["session_date"], plan["source_kind"])
    target = paths.writable(paths.levels / name)
    meta_path = paths.writable(paths.levels / meta_name)
    if target.exists():
        try:
            existing = sha256_bytes(target.read_bytes())
        except OSError as exc:
            message = f"Refused: {name} is already added and cannot be read ({exc}). Nothing was changed."
            out(message)
            return AddResult(REFUSED, message, target, meta_path, warnings, digest, io_error=True)
        if existing == digest:
            if not meta_path.exists():  # an earlier add stopped between the level file and its sidecar
                _write_meta(paths, meta_path, src, plan, digest, warnings, now)
            message = f"Already added: {name} holds exactly this file. Nothing was changed."
            out(message)
            return AddResult(ALREADY, message, target, meta_path, warnings, digest)
        message = (f"Refused: a different file is already added as {name}. Scout never edits or replaces an "
                   f"added level file. Nothing was changed.")
        out(message)
        return AddResult(REFUSED, message, target, meta_path, warnings, digest)

    try:
        agreed = bool(confirm(f"Add {src.name} as {name}? [y/N] "))
    except (EOFError, KeyboardInterrupt):
        agreed = False
    if not agreed:
        message = "Not added. Nothing was copied."
        out(message)
        return AddResult(DECLINED, message, None, None, warnings, digest)

    try:
        _write_new(paths, target, body)
    except FileExistsError:  # another add won the race for this name: answer from what is there now
        return add_file(paths, src, now, confirm=lambda _p: False, out=out)
    _write_meta(paths, meta_path, src, plan, digest, warnings, now)
    message = f"Added {src.name} as {name} (sha256 {digest[:12]}...)."
    out(message)
    return AddResult(ADDED, message, target, meta_path, warnings, digest)


def _plan_from_bytes(body: bytes, name: str) -> dict:
    """`load_plan` over bytes already read (the same rules and errors)."""
    try:
        data = json.loads(body.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:
        raise JasonPlanError(f"{name} is unreadable: {exc}") from exc
    return plan_from_data(data, name=name)


def _write_meta(paths: ScoutPaths, meta_path: Path, src: Path, plan: dict, digest: str,
                warnings: tuple[str, ...], now: datetime) -> None:
    meta = LevelMeta(added_at=iso(now), sha256=digest, original_filename=src.name,
                     session_date=plan["session_date"], source_kind=plan["source_kind"], warnings=warnings)
    atomic_write_json(paths, meta_path, meta.to_record())


def _write_new(paths: ScoutPaths, target: Path, body: bytes) -> None:
    """Write `body` to a new file `target` in one step: a temp file, then a hard link (or, on a Windows
    file system without links, a rename) that fails when `target` exists. Never replaces a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.writable(target.with_name(f".{target.name}.{os.getpid()}.tmp"))
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
        if target.exists():
            raise FileExistsError(str(target))
        try:
            os.link(tmp, target)  # atomic, and refuses an existing name on every OS
        except FileExistsError:
            raise
        except OSError:  # pragma: no cover - FAT or exFAT have no hard links
            if os.name != "nt":
                raise
            os.rename(tmp, target)  # Windows: fails if target exists
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover
                pass


def newest_inbox_file(paths: ScoutPaths) -> Path | None:
    """The newest `*.json` in `inbox\\jason\\` (what Add Levels takes when no file is dropped on it)."""
    try:
        files = [p for p in paths.inbox_jason.iterdir() if p.is_file() and p.suffix.lower() == ".json"]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


# --------------------------------------------------------------------------- auto-add


def added_digests(paths: ScoutPaths) -> set[str]:
    """sha256 of every added level file (from the sidecars)."""
    out: set[str] = set()
    try:
        metas = [p for p in paths.levels.iterdir() if p.name.endswith(META_SUFFIX)]
    except OSError:
        return out
    for p in metas:
        try:
            sha = json.loads(p.read_text(encoding="utf-8")).get("sha256")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(sha, str):
            out.add(sha)
    return out


def rejected_note(src: Path) -> Path:
    return src.with_name(src.name + REJECTED_SUFFIX)


def _note_digest(note: Path) -> str | None:
    try:
        for line in note.read_text(encoding="utf-8").splitlines():
            if line.startswith("sha256: "):
                return line[len("sha256: "):].strip()
    except OSError:
        return None
    return None


def _write_rejected(paths: ScoutPaths, src: Path, result: AddResult, now: datetime) -> None:
    """`<file>.rejected.txt` next to a refused file, when that file is inside Scout's folder. The note
    names the sha256 of the bytes that were checked; an I/O refusal (nothing checked) writes no note, so
    a file that was locked or still being written is tried again."""
    if result.io_error or result.digest is None:
        return
    digest, message = result.digest, result.message
    try:
        note = paths.writable(rejected_note(src))
    except (OwnFolderError, OSError):
        return
    text = (f"REFUSED: {_one_line(message)}\nat: {iso(now)}\nfile: {src.name}\nsha256: {digest}\n"
            "Scout did not add this file. Write a corrected file (new bytes), or ask the owner.\n")
    try:
        atomic_write_text(paths, note, text)
    except (OwnFolderError, OSError):  # pragma: no cover
        pass


def inbox_files(paths: ScoutPaths) -> list[Path]:
    """The candidate level files in `inbox\\jason` by name: `*.json`, not hidden (temp files are not
    `.json`)."""
    try:
        return sorted((p for p in paths.inbox_jason.iterdir()
                       if p.is_file() and p.suffix.lower() == ".json" and not p.name.startswith(".")),
                      key=lambda p: p.name)
    except OSError:
        return []


def add_inbox(paths: ScoutPaths, now: datetime, *, out: Out = print) -> tuple[list[AddResult], int]:
    """Add every inbox file not already added (by sha256) and not already refused in this form.
    Returns (the results of the files tried, the number skipped)."""
    done = added_digests(paths)
    results: list[AddResult] = []
    skipped = 0
    for src in inbox_files(paths):
        try:
            digest = sha256_bytes(src.read_bytes())
        except OSError:
            continue
        if digest in done or _note_digest(rejected_note(src)) == digest:
            skipped += 1
            continue
        out(f"--- {src.name}")
        result = add_file(paths, src, now, confirm=lambda _p: True, out=out, auto=True, inbox_only=True)
        if result.status == REFUSED:
            _write_rejected(paths, src, result, now)
            out(f"REFUSED: {src.name}: {_one_line(result.message)}")
        else:
            done.add(digest)
        results.append(result)
    return results, skipped


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cli_levels_add(ctx: Any, args: Any) -> int:
    """`levels add [file] [--yes] [--inbox]`: 0 added or already added (or declined), 2 refused or no
    file. A refusal's last line starts "REFUSED:"."""
    now = ctx.clock.now()
    if getattr(args, "inbox", False):
        if getattr(args, "file", None):
            print("REFUSED: give a file or --inbox, not both.")
            return 2
        results, skipped = add_inbox(ctx.paths, now)
        added = sum(1 for r in results if r.status == ADDED)
        refused = sum(1 for r in results if r.status == REFUSED)
        for r in results:
            if r.status == ADDED:
                ctx.log.info("levels add --inbox: %s", r.message)
        print(f"Inbox: {added} added, {refused} refused, {len(results) - added - refused} already added, "
              f"{skipped} skipped (added or refused before).")
        return 2 if refused else 0
    raw = getattr(args, "file", None)
    src = Path(raw) if raw else newest_inbox_file(ctx.paths)
    if src is None:
        print(f"REFUSED: no level file given and none found in {ctx.paths.inbox_jason}.")
        return 2
    auto = bool(getattr(args, "yes", False))
    confirm: Confirm = (lambda _prompt: True) if auto else _ask
    result = add_file(ctx.paths, src, now, confirm=confirm, auto=auto)
    if result.status == ADDED:
        ctx.log.info("levels add: %s", result.message)
    if result.status == REFUSED:
        _write_rejected(ctx.paths, src, result, now)
        print(f"REFUSED: {src.name}: {_one_line(result.message)}")
        return 2
    return 0
