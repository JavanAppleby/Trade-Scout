"""Tail Radar's evidence packs (READS_TEST.md 4.1, 5): open, read the new lines, close.

Radar appends `<radar_dir>/<radar_packs_subdir>/<trading day>.packs.jsonl` while Scout reads it. On
Windows the file is opened with every share flag (read, write and delete), so Scout's read can never
make Radar's append, rename or clean-up fail; the handle is held only for one read. Elsewhere a
plain read-only open does the same.

`PackTailer.poll(days)` returns the packs added since the last poll, in file order
(`poll_spans` also gives each pack's file and byte span). Only complete lines are taken (a last line
with no newline is left for the next poll); a line that does not parse, is not an object, or is not
`format` 1 `kind` "pack" with a `pack_id` is skipped. How far each file was read is kept in
`state/reads/_tail.json`, written atomically after every read that moved it, or, with `save=False`,
when the caller `commit`s a position once it has handled the packs before it (the reads service
commits up to the start of the earliest pack it has not finished, so a pack read but not yet handled
when Scout stops is read again at the next start; the service's own record of handled packs keeps
it from being read twice). `cursor_days()` names the days of the files in the cursor, so the first
poll after a start can finish an earlier day's file. A file that became shorter than the saved
position (rewritten or restored) is read again from its start, logged once; the caller's record of
handled packs keeps those packs from being read twice.

Scout never writes Radar's folder: this module only reads it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import IO, Any

from scout.config import ScoutConfig
from scout.paths import ScoutPaths
from scout.store import atomic_write_json, read_json

MAX_READ_BYTES = 8_000_000  # at most this much of one file per poll; the rest is read next time
KEEP_CURSOR_DAYS = 10

__all__ = ["MAX_READ_BYTES", "PackTailer", "Polled", "open_shared", "packs_file"]


def open_shared(path: Path) -> IO[bytes]:
    """`path` opened for reading only, sharing read, write and delete with every other process."""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes  # noqa: PLC0415 - Windows only
    import msvcrt  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                       wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    generic_read, share_all, open_existing, normal = 0x80000000, 0x1 | 0x2 | 0x4, 3, 0x80
    handle = create(str(path), generic_read, share_all, None, open_existing, normal, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        err = ctypes.get_last_error()
        if err in (2, 3):  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
            raise FileNotFoundError(str(path))
        raise OSError(f"CreateFileW failed ({err})")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise
    return os.fdopen(fd, "rb")


def packs_file(cfg: ScoutConfig, day: date) -> Path | None:
    """Radar's packs file for `day`, or None when `radar_dir` is empty."""
    if not cfg.radar_dir.strip():
        return None
    folder = (cfg.scout_dir / cfg.radar_dir).resolve(strict=False)
    return folder / cfg.reads.packs_subdir / f"{day.isoformat()}.packs.jsonl"


def _pack(line: bytes) -> dict[str, Any] | None:
    try:
        rec = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    fmt = rec.get("format")
    if fmt != 1 or isinstance(fmt, bool) or rec.get("kind") != "pack":
        return None
    if not isinstance(rec.get("pack_id"), str) or not isinstance(rec.get("symbol"), str):
        return None
    return rec


@dataclass(frozen=True)
class Polled:
    """One pack and where it lies: the file's name and the byte span of its line."""

    pack: dict[str, Any]
    name: str
    start: int
    end: int


class PackTailer:
    def __init__(self, cfg: ScoutConfig, paths: ScoutPaths, log: logging.Logger | None = None) -> None:
        self.cfg, self.paths = cfg, paths
        self.log = log or logging.getLogger("scout.reads")
        self.cursor_file = paths.reads / "_tail.json"
        self.offsets: dict[str, int] = self._load()
        self._warned: set[str] = set()

    def _load(self) -> dict[str, int]:
        rec = read_json(self.cursor_file) or {}
        files = rec.get("files") if isinstance(rec, dict) else None
        out: dict[str, int] = {}
        for name, value in (files or {}).items():
            if isinstance(name, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[name] = value
        return out

    def _save(self, offsets: dict[str, int]) -> None:
        names = sorted(offsets)[-KEEP_CURSOR_DAYS:]
        atomic_write_json(self.paths, self.cursor_file, {"format": 1, "files": {n: offsets[n] for n in names}})

    def commit(self, offsets: dict[str, int]) -> None:
        """Save a position returned by `position()` (never raises)."""
        try:
            self._save(offsets)
        except OSError as exc:
            self.log.warning("reads: could not save how far Radar's packs were read (%s)", type(exc).__name__)

    def position(self) -> dict[str, int]:
        return dict(self.offsets)

    def cursor_days(self) -> list[date]:
        """The days of the packs files named in the cursor."""
        out = []
        for name in self.offsets:
            try:
                out.append(date.fromisoformat(name.split(".", 1)[0]))
            except ValueError:
                continue
        return sorted(out)

    def poll(self, days: Iterable[date], *, save: bool = True) -> list[dict[str, Any]]:
        return [p.pack for p in self.poll_spans(days, save=save)]

    def poll_spans(self, days: Iterable[date], *, save: bool = True) -> list[Polled]:
        out: list[Polled] = []
        moved = False
        for day in sorted(set(days)):
            path = packs_file(self.cfg, day)
            if path is None:
                continue
            got, new_offset = self._read(path)
            if new_offset != self.offsets.get(path.name, 0):
                self.offsets[path.name] = new_offset
                moved = True
            out.extend(got)
        if moved and save:
            self.commit(self.offsets)
        return out

    def _read(self, path: Path) -> tuple[list[Polled], int]:
        offset = self.offsets.get(path.name, 0)
        try:
            with open_shared(path) as fh:
                size = fh.seek(0, os.SEEK_END)
                if size < offset:
                    if path.name not in self._warned:
                        self._warned.add(path.name)
                        self.log.warning("reads: Radar's %s is shorter than when last read; reading it again from "
                                         "its start (packs already handled are not read twice)", path.name)
                    offset = 0
                fh.seek(offset)
                data = fh.read(min(size - offset, MAX_READ_BYTES))
        except FileNotFoundError:
            return [], offset
        except OSError as exc:
            if path.name not in self._warned:
                self._warned.add(path.name)
                self.log.warning("reads: could not read Radar's %s (%s)", path.name, type(exc).__name__)
            return [], offset
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        out: list[Polled] = []
        at = offset
        for line in data[: end + 1].split(b"\n")[:-1]:
            start, at = at, at + len(line) + 1
            rec = _pack(line) if line.strip() else None
            if rec is not None:
                out.append(Polled(rec, path.name, start, at))
        return out, offset + end + 1
