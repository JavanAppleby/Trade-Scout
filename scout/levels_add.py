"""Add Levels: validate the owner's level file and file it, never editing it (SCOUT_SPEC 5.2, DESIGN 2.4).

`python -m scout levels add <file> [--yes]` (the **Add Levels** launcher):
1. validate with the loader (`scout.levels.load_plan`); a file that raises is refused and nothing
   is copied;
2. print a summary and every warning;
3. ask once (`--yes` skips the question);
4. copy the file byte-for-byte to `state\\levels\\<session_date>.<source_kind>.json` and write the
   sidecar `<session_date>.<source_kind>.meta.json` (`added_at`, `sha256`, `original_filename`,
   `session_date`, `source_kind`, `warnings`).

An added file is never edited or overwritten: the same bytes again is "already added" (nothing
written); different bytes for the same session date and kind are refused.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from scout.clock import iso
from scout.levels import JasonPlanError, load_plan
from scout.models import LevelMeta
from scout.paths import ScoutPaths
from scout.store import atomic_write_json, sha256_bytes

__all__ = ["AddResult", "add_file", "cli_levels_add", "newest_inbox_file", "summary_lines", "target_names"]

ADDED, ALREADY, REFUSED, DECLINED = "added", "already_added", "refused", "declined"

Confirm = Callable[[str], bool]
Out = Callable[[str], Any]


@dataclass(frozen=True)
class AddResult:
    status: str  # added | already_added | refused | declined
    message: str
    target: Path | None = None
    meta: Path | None = None
    warnings: tuple[str, ...] = field(default=())

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


def add_file(paths: ScoutPaths, src: Path | str, now: datetime, *, confirm: Confirm,
             out: Out = print) -> AddResult:
    """Validate `src`, show its warnings, ask once, then copy it and its sidecar into `paths.levels`."""
    src = Path(src)
    try:
        plan = load_plan(src)
        body = src.read_bytes()
    except (JasonPlanError, OSError) as exc:
        message = f"Refused: {exc}. Nothing was copied."
        out(message)
        return AddResult(REFUSED, message)

    warnings = tuple(plan["warnings"])
    for line in summary_lines(plan):
        out(line)
    if warnings:
        out(f"Warnings ({len(warnings)}):")
        for w in warnings:
            out(f"  - {w}")
    else:
        out("Warnings: none")

    digest = sha256_bytes(body)
    name, meta_name = target_names(plan["session_date"], plan["source_kind"])
    target = paths.writable(paths.levels / name)
    meta_path = paths.writable(paths.levels / meta_name)
    if target.exists():
        try:
            existing = sha256_bytes(target.read_bytes())
        except OSError as exc:
            message = f"Refused: {name} is already added and cannot be read ({exc}). Nothing was changed."
            out(message)
            return AddResult(REFUSED, message, target, meta_path, warnings)
        if existing == digest:
            message = f"Already added: {name} holds exactly this file. Nothing was changed."
            out(message)
            return AddResult(ALREADY, message, target, meta_path, warnings)
        message = (f"Refused: a different file is already added as {name}. Scout never edits or replaces an "
                   f"added level file. Nothing was changed.")
        out(message)
        return AddResult(REFUSED, message, target, meta_path, warnings)

    try:
        agreed = bool(confirm(f"Add {src.name} as {name}? [y/N] "))
    except (EOFError, KeyboardInterrupt):
        agreed = False
    if not agreed:
        message = "Not added. Nothing was copied."
        out(message)
        return AddResult(DECLINED, message, None, None, warnings)

    _write_new(paths, target, body)
    meta = LevelMeta(added_at=iso(now), sha256=digest, original_filename=src.name,
                     session_date=plan["session_date"], source_kind=plan["source_kind"], warnings=warnings)
    atomic_write_json(paths, meta_path, meta.to_record())
    message = f"Added {src.name} as {name} (sha256 {digest[:12]}...)."
    out(message)
    return AddResult(ADDED, message, target, meta_path, warnings)


def _write_new(paths: ScoutPaths, target: Path, body: bytes) -> None:
    """Write `body` to a new file `target`: temp file first, then a rename that never replaces."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.writable(target.with_name(f".{target.name}.{os.getpid()}.tmp"))
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
        if target.exists():  # pragma: no cover - checked by the caller; a guard against a race
            raise FileExistsError(str(target))
        os.rename(tmp, target)  # Windows: fails if target exists; never replaces an added file
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


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cli_levels_add(ctx: Any, args: Any) -> int:
    """`levels add [file] [--yes]`: 0 added or already added (or declined), 2 refused or no file."""
    raw = getattr(args, "file", None)
    src = Path(raw) if raw else newest_inbox_file(ctx.paths)
    if src is None:
        print(f"No level file given and none found in {ctx.paths.inbox_jason}.")
        return 2
    confirm: Confirm = (lambda _prompt: True) if getattr(args, "yes", False) else _ask
    result = add_file(ctx.paths, src, ctx.clock.now(), confirm=confirm)
    if result.status == ADDED:
        ctx.log.info("levels add: %s", result.message)
    return 2 if result.status == REFUSED else 0
