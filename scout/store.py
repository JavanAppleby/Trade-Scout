"""Atomic and append-only file IO (SCOUT_SPEC 10, DESIGN 2.1).

- Single files: written to a temp file in the same folder, then `os.replace`d, retried while a
  reader holds the target open on Windows (Guardian's heartbeat pattern; exit_lessons 9). The temp
  file is removed on failure.
- `.jsonl` files: append-only, one `write` of one complete line ending in "\\n". Every record must
  carry `format`. Readers ignore a last line without a newline (`scout_reader.records.read_jsonl`).
- Every path goes through `ScoutPaths.writable` first.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from scout_reader import records as _records

from scout.paths import ScoutPaths

REPLACE_TRIES = 20
REPLACE_PAUSE_S = 0.05

__all__ = [
    "REPLACE_PAUSE_S", "REPLACE_TRIES", "append_jsonl", "atomic_write_json", "atomic_write_text",
    "canonical_json", "read_json", "read_jsonl", "sha256_bytes", "sha256_text",
]


def atomic_write_text(paths: ScoutPaths, path: Path | str, text: str, *,
                      pause: Callable[[float], None] = time.sleep) -> None:
    """Write `text` (UTF-8) to `path` atomically: temp file, then os.replace with retries."""
    target = paths.writable(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.writable(target.with_name(f".{target.name}.{os.getpid()}.tmp"))
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:  # Windows: a reader has the target open for a moment
                if attempt == REPLACE_TRIES - 1:
                    raise
                pause(REPLACE_PAUSE_S)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def atomic_write_json(paths: ScoutPaths, path: Path | str, obj: Any, **kwargs: Any) -> None:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1) + "\n"
    atomic_write_text(paths, path, text, **kwargs)


def append_jsonl(paths: ScoutPaths, path: Path | str, record: Mapping[str, Any]) -> None:
    """Append one record as one line. The record must carry an integer `format`."""
    fmt = record.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        raise ValueError("every record must carry an integer 'format'")
    line = json.dumps(dict(record), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if "\n" in line:  # json.dumps escapes newlines inside strings; this is a guard only
        raise ValueError("a JSONL record must be one line")  # pragma: no cover
    target = paths.writable(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    with open(target, "a+b") as fh:  # one open: "a+" appends and still lets the last byte be read
        _repair_partial_tail(fh)
        fh.write(data)


def _repair_partial_tail(fh: Any) -> None:
    """If a crash left a last line without "\\n", end it so the new record starts on its own line
    (the broken line stays unparseable and is skipped by readers). `fh` is open "a+b"."""
    size = fh.seek(0, os.SEEK_END)
    if size == 0:
        return
    fh.seek(size - 1)
    last = fh.read(1)
    fh.seek(0, os.SEEK_END)
    if last != b"\n":
        fh.write(b"\n")


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    return _records.read_jsonl(path)


def read_json(path: Path | str) -> dict[str, Any] | None:
    return _records.read_json(path)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def canonical_json(obj: Any) -> str:
    """Sorted keys, no spaces: the text hashes are taken over."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
