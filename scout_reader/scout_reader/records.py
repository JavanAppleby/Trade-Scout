"""Tolerant reading of Scout's files, the format check, the as-of filter and Result (DESIGN 3.2-3.3).

Nothing here raises into a caller for a missing, partial or corrupt file: a missing file reads as
empty, a last line without a newline is ignored (a writer may be mid-append), a line that fails to
parse is skipped, and a record with a missing or unknown `format` is dropped by `known()`.
Stdlib only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scout_reader._et import utc_to_et

KNOWN_FORMATS: frozenset[int] = frozenset({1})
DEFAULT_STALE_S = 120.0

# File and folder names under Scout's folder (DESIGN 5); every reader and writer uses these.
STATE = "state"
FACTS = "state/facts"
HEALTH = "state/health"
BEATS = "state/beats"
ANSWERS = "state/answers"
LABELS = "state/labels"
LLM = "state/llm"
MACRO = "state/macro"
MACRO_HISTORY = "state/macro/history.jsonl"
RAW = "state/raw"
EDGAR_DUE = "state/edgar/due"
JASON = "state/jason"
JASON_SNAPSHOTS = "state/jason/snapshots.jsonl"
VERIFY = "state/verify"
PARAMS = "state/params.jsonl"
DISCORD = "state/discord"
HEARTBEAT = "state/scout.heartbeat"

__all__ = [
    "ANSWERS", "BEATS", "DEFAULT_STALE_S", "DISCORD", "EDGAR_DUE", "FACTS", "HEALTH", "HEARTBEAT", "JASON",
    "JASON_SNAPSHOTS", "KNOWN_FORMATS", "LABELS", "LLM", "MACRO", "MACRO_HISTORY", "PARAMS", "RAW", "STATE",
    "UNKNOWN", "VERIFY", "day_file", "day_files", "governing_beat", "known", "last_params", "parse_time",
    "read_json", "read_jsonl", "result", "visible",
]


# --------------------------------------------------------------------------- reading


def read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every complete, parseable JSON-object line of `path`; [] when it is missing or unreadable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    lines = data.split(b"\n")
    for raw in lines[:-1]:  # the part after the last "\n" is a partial line (or empty)
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_json(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The JSON object in `path`, or None when missing, unreadable or not an object."""
    try:
        with open(path, "rb") as fh:
            rec = json.loads(fh.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    return rec if isinstance(rec, dict) else None


def known(rec: Mapping[str, Any]) -> bool:
    """True when the record carries a `format` this reader understands."""
    fmt = rec.get("format")
    return isinstance(fmt, int) and not isinstance(fmt, bool) and fmt in KNOWN_FORMATS


# --------------------------------------------------------------------------- time


def parse_time(value: object) -> datetime | None:
    """Aware UTC datetime from a stored "...Z" (or offset) string; None when unreadable or naive."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def visible(rec: Mapping[str, Any], as_of: datetime) -> bool:
    """SS 2 rule 9: the record counts at `as_of` only when `first_seen_at <= as_of` and, where
    present, `published_at` and `labelled_at` are <= as_of. A record without a readable
    `first_seen_at` (or `labelled_at` for labels) is never visible."""
    seen = parse_time(rec.get("first_seen_at")) or parse_time(rec.get("labelled_at"))
    if seen is None or seen > as_of:
        return False
    for key in ("published_at", "labelled_at"):
        if rec.get(key) is not None:
            at = parse_time(rec.get(key))
            if at is None or at > as_of:
                return False
    return True


# --------------------------------------------------------------------------- daily files


def day_file(scout_dir: str | os.PathLike[str], folder: str, day: date, ext: str = ".jsonl") -> Path:
    return Path(scout_dir) / folder / f"{day.isoformat()}{ext}"


def day_files(folder: str | os.PathLike[str], start: datetime, end: datetime, ext: str = ".jsonl") -> list[Path]:
    """The ET-date files of `folder` that can hold records timed in [start, end] (aware)."""
    first, last = utc_to_et(start).date(), utc_to_et(end).date()
    out: list[Path] = []
    day = first
    while day <= last and len(out) < 3700:
        out.append(Path(folder) / f"{day.isoformat()}{ext}")
        day += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- params and beats


def last_params(scout_dir: str | os.PathLike[str], as_of: datetime) -> dict[str, Any] | None:
    """The last `params.jsonl` record with first_seen_at <= as_of (DESIGN 3.2 rule 3)."""
    best: dict[str, Any] | None = None
    best_at: datetime | None = None
    for rec in read_jsonl(Path(scout_dir) / PARAMS):
        if not known(rec):
            continue
        at = parse_time(rec.get("first_seen_at"))
        if at is None or at > as_of:
            continue
        if best_at is None or at >= best_at:
            best, best_at = rec, at
    return best


def governing_beat(scout_dir: str | os.PathLike[str], as_of: datetime,
                   stale_s: float = DEFAULT_STALE_S) -> dict[str, Any] | None:
    """The latest beat with `at` in (as_of - stale_s, as_of], or None when there is none or that
    beat says `stopped` (DESIGN 3.2 rule 2: Scout is then stale)."""
    start = as_of - timedelta(seconds=float(stale_s))
    best: dict[str, Any] | None = None
    best_at: datetime | None = None
    for path in day_files(Path(scout_dir) / BEATS, start, as_of):
        for rec in read_jsonl(path):
            if not known(rec):
                continue
            at = parse_time(rec.get("at"))
            if at is None or not (start < at <= as_of):
                continue
            if best_at is None or at >= best_at:
                best, best_at = rec, at
    if best is None or best.get("stopped") is not False:
        return None
    return best


# --------------------------------------------------------------------------- results


def result(value: str, evidence: Iterable[str] = (), stale: bool = False,
           reasons: Iterable[str] = ()) -> dict[str, Any]:
    """{"value", "evidence", "stale", "reasons"}; a value other than "true"/"false" is "unknown"."""
    if value not in ("true", "false"):
        value = "unknown"
    return {"value": value, "evidence": [str(e) for e in evidence], "stale": bool(stale),
            "reasons": [str(r) for r in reasons]}


def UNKNOWN(reason: str, *, stale: bool = False) -> dict[str, Any]:  # noqa: N802 - DESIGN's name
    return result("unknown", (), stale, (reason,))
