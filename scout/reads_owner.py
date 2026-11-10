"""The owner's own reads (READS_TEST.md 6): `Add Read.bat`, the `inbox\\reads` drop and the pick-up.

`python -m scout reads add` asks four things when run with no arguments (the ticker, long or short,
strength 1 to 5, and why, which may be left empty) and also takes them as arguments:
`reads add ABCD long 4 "why"`. It writes one small file in `inbox\\reads\\`, first as
`<name>.json.tmp`, then renamed to `<name>.json`, so Scout never reads half a file:

    {"format": 1, "kind": "owner_note", "symbol": "ABCD", "direction": "long", "strength": 4,
     "why": "free text or null", "noted_at": "<UTC time the note was written>"}

The running Scout picks every `*.json` there up within `reads_poll_s` (`pick_up`): it appends a
line with `kind` "owner", `reader` "owner", `noted_at` from the note and `first_seen_at` when Scout
picked it up, to the reads file of the note's trading day, then renames the note to
`<name>.json.done`. A note that is not valid is renamed to `<name>.json.rejected` and logged. The
line carries the note's sha256 (`note_sha256`), so a note picked up just before a crash is never
added twice. The free text is stored as data and never sent to a model.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from scout_reader.gates import trading_day_of

from scout.calendar import session_at
from scout.clock import parse_iso
from scout.config import ScoutConfig
from scout.paths import ScoutPaths
from scout.reads_file import FORMAT, ReadsWriter, iso_us, reads_on
from scout.running import is_running
from scout.store import atomic_write_text, sha256_bytes

WHY_CHARS = 500
_TICKER = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")

__all__ = ["cli_reads_add", "note_problem", "owner_line", "pick_up", "write_note"]


def note_problem(note: Any) -> str | None:
    """Why a note is not valid, or None."""
    if not isinstance(note, dict) or note.get("format") != 1 or note.get("kind") != "owner_note":
        return "not an owner note of format 1"
    if not isinstance(note.get("symbol"), str) or not _TICKER.fullmatch(note["symbol"]):
        return "the ticker must be 1 to 10 capital letters, digits, '.' or '-', starting with a letter"
    if note.get("direction") not in ("long", "short"):
        return "the direction must be long or short"
    s = note.get("strength")
    if not isinstance(s, int) or isinstance(s, bool) or not 1 <= s <= 5:
        return "the strength must be a whole number from 1 to 5"
    if note.get("why") is not None and not isinstance(note.get("why"), str):
        return "why must be text"
    if parse_iso(note.get("noted_at")) is None:
        return "noted_at must be a UTC time"
    return None


def owner_line(note: dict[str, Any], digest: str) -> dict[str, Any]:
    noted = parse_iso(note["noted_at"])
    assert noted is not None
    why = note.get("why")
    return {
        "format": FORMAT, "kind": "owner", "read_id": "", "reader": "owner", "model": None, "effort": None,
        "prompt_version": None, "symbol": note["symbol"], "trading_day": trading_day_of(noted).isoformat(),
        "session": str(session_at(noted)), "candidate_id": None, "pack_id": None, "pack_made_at": None,
        "ref_bid": None, "ref_ask": None, "direction": note["direction"], "strength": note["strength"],
        "horizon_min": None, "invalidation": None, "target": None, "reasons": [], "fade_if": None, "status": "new",
        "parent_read_id": None, "facts_used": [], "usage": None, "cost_usd": "0.0",
        "noted_at": iso_us(noted), "note": why.strip()[:WHY_CHARS] if isinstance(why, str) and why.strip() else None,
        "note_sha256": digest,
    }


def _rename(paths: ScoutPaths, src: Path, suffix: str) -> None:
    os.replace(paths.writable(src), paths.writable(src.with_name(src.name + suffix)))


def pick_up(cfg: ScoutConfig, paths: ScoutPaths, writer: ReadsWriter, now: datetime, *,
            log: logging.Logger | None = None) -> int:
    """Add every note in `inbox\\reads`; returns how many were added. Never raises."""
    log = log or logging.getLogger("scout.reads")
    try:
        notes = sorted(p for p in paths.inbox_reads.iterdir() if p.is_file() and p.name.endswith(".json"))
    except OSError:
        return 0
    added = 0
    for src in notes:
        try:
            body = src.read_bytes()
        except OSError:
            continue  # being written or locked: the next poll reads it
        digest = sha256_bytes(body)
        try:
            note = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            note = None
        problem = note_problem(note)
        try:
            if problem is not None:
                log.warning("reads: the owner's note %s was not added: %s", src.name, problem)
                _rename(paths, src, ".rejected")
                continue
            line = owner_line(note, digest)
            seen = {r.get("note_sha256") for r in reads_on(paths, line["trading_day"]) if r.get("kind") == "owner"}
            if digest not in seen:
                writer.append(line, number=True)
                added += 1
            _rename(paths, src, ".done")
        except OSError as exc:
            log.warning("reads: the owner's note %s: %s", src.name, type(exc).__name__)
    return added


def write_note(paths: ScoutPaths, symbol: str, direction: str, strength: int, why: str | None,
               now: datetime) -> Path:
    """Drop one note in `inbox\\reads` (written as .tmp, then renamed). Raises ValueError when invalid."""
    note = {"format": 1, "kind": "owner_note", "symbol": symbol.strip().upper(), "direction": direction.strip().lower(),
            "strength": strength, "why": (why or "").strip()[:WHY_CHARS] or None, "noted_at": iso_us(now)}
    problem = note_problem(note)
    if problem is not None:
        raise ValueError(problem)
    name = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{note['symbol']}.json"
    target = paths.writable(paths.inbox_reads / name)
    atomic_write_text(paths, target, json.dumps(note, ensure_ascii=False, sort_keys=True) + "\n")
    return target


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def cli_reads_add(ctx: Any, args: Any) -> int:
    """`reads add [TICKER long|short STRENGTH [WHY...]]`: 0 noted, 2 bad input."""
    given = list(getattr(args, "fields", None) or [])
    if given and len(given) < 3:
        print("REFUSED: give the ticker, long or short, and the strength (and why, if you like), or nothing.")
        return 2
    if given:
        symbol, direction, strength_text, why = given[0], given[1], given[2], " ".join(given[3:])
    else:
        symbol = _ask("Ticker: ")
        direction = _ask("Long or short: ")
        strength_text = _ask("Strength 1 to 5 (5: take it now at full size; 1: avoid): ")
        why = _ask("Why (optional, press Enter to skip): ")
    try:
        strength = int(strength_text)
    except ValueError:
        print("REFUSED: the strength must be a whole number from 1 to 5.")
        return 2
    try:
        path = write_note(ctx.paths, symbol, direction, strength, why, ctx.clock.now())
    except ValueError as exc:
        print(f"REFUSED: {exc}.")
        return 2
    print(f"Noted: {symbol.strip().upper()} {direction.strip().lower()} strength {strength} ({path.name}).")
    if not ctx.cfg.reads.enabled:
        print("Reads are off (reads_enabled = false): the note waits in inbox\\reads, and Scout adds it once "
              "reads are turned on and Scout runs.")
    elif is_running(ctx.paths.root):
        print("The running Scout adds it to today's reads within a few seconds.")
    else:
        print("Scout is not running: the note waits in inbox\\reads, and Scout adds it when it next starts "
              "(the read is timed then; noted_at keeps the time you wrote it).")
    return 0
