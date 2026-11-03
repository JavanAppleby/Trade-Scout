"""A per-Reader cache of Scout's append-only files, for `entry_blocks` (owner, 2026-11-04; SCOUT_SPEC
10.3; DESIGN 3.9). Stdlib only. Radar asks for blocks every time a mover passes its checks, and a
facts day file is about 10 MB, so reading it afresh each time took 0.2-0.8 s.

A view of a file is parsed once and kept, keyed on the file's path, size and modification time. A
view keeps only what its caller needs (`project` turns each record into an item, or None to drop
it) under a group key (`key`), so memory stays small. When the file has grown, only the new bytes are
read: Scout's files are append-only (SS 2 rule 9), and the last 4 KiB before the point already read
must still be the same bytes, else the file is read again from the start. A shrunk file is read
again from the start too. The damage rules are `scout_reader.api`'s (DESIGN 3.2 rule 4): a line that
does not parse, a record with an unknown `format`, and a last line without a newline are noted, and
the part after the last newline is read again next time (a writer may be mid-append).

Every read is open-read-close. A missing file is None; any other OSError reaches the caller, which
notes the file as unreadable. A lock keeps one Reader safe across threads.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

from scout_reader.records import known

ANCHOR_BYTES = 4096  # bytes before the point already read that must not have changed
MAX_VIEWS = 96  # views kept per Reader (least recently used dropped first)

__all__ = ["ANCHOR_BYTES", "MAX_VIEWS", "FileCache", "View"]

Project = Callable[[dict[str, Any]], Any]
Key = Callable[[Any], Hashable]


class View:
    """One file seen through one projection: `groups` maps a key to its items in file order,
    `damage` holds "unreadable_line" / "unknown_format" for complete lines, `partial` is a last line
    without a newline."""

    __slots__ = ("anchor", "damage", "dirty", "groups", "mtime_ns", "offset", "partial", "size")

    def __init__(self) -> None:
        self.groups: dict[Hashable, list[Any]] = {}
        self.damage: set[str] = set()
        self.partial = False
        self.size = -1
        self.mtime_ns = -1
        self.offset = 0  # bytes of complete lines read so far
        self.anchor = b""  # sha256 of the ANCHOR_BYTES before `offset`
        self.dirty: set[Hashable] = set()  # groups whose items arrived out of order

    def notes(self) -> set[str]:
        return self.damage | ({"partial_line"} if self.partial else set())


def parse_into(view: View, data: bytes, project: Project, key: Key, order: Callable[[Any], Any] | None) -> int:
    """Add the complete lines of `data` to `view`; the number of bytes they take."""
    end = data.rfind(b"\n") + 1
    for raw in data[:end].split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            view.damage.add("unreadable_line")
            continue
        if not isinstance(rec, dict):
            view.damage.add("unreadable_line")
            continue
        if not known(rec):
            view.damage.add("unknown_format")
        item = project(rec)
        if item is None:
            continue
        group = view.groups.setdefault(key(item), [])
        if order is not None and group and order(item) < order(group[-1]):
            view.dirty.add(key(item))
        group.append(item)
    view.partial = bool(data[end:].strip())
    return end


class FileCache:
    """Views of files, keyed on (path, view name). `load` returns the up-to-date View or None."""

    def __init__(self, max_views: int = MAX_VIEWS) -> None:
        self._views: OrderedDict[tuple[str, str], View] = OrderedDict()
        self._lock = threading.Lock()
        self.max_views = max_views
        self.full_reads = 0  # for tests: how often a file was read from the start
        self.tail_reads = 0  # ... and how often only its new bytes were read

    def clear(self) -> None:
        with self._lock:
            self._views.clear()

    def load(self, path: str | os.PathLike[str], name: str, project: Project, key: Key,
             order: Callable[[Any], Any] | None = None) -> View | None:
        """The view `name` of `path`. `order` (optional) is the sort key of a group's items: a group
        that received an item out of order is sorted again."""
        where = (os.fspath(path), name)
        with self._lock:
            try:
                st = os.stat(where[0])
            except FileNotFoundError:
                self._views.pop(where, None)
                return None
            old = self._views.get(where)
            if old is not None and old.size == st.st_size and old.mtime_ns == st.st_mtime_ns:
                self._views.move_to_end(where)
                return old
            view = self._grow(old, where[0], st.st_size, project, key, order) if old is not None else None
            if view is None:
                view = self._full(where[0], project, key, order)
            view.size, view.mtime_ns = st.st_size, st.st_mtime_ns
            for group in view.dirty:
                if order is not None:
                    view.groups[group].sort(key=order)
            view.dirty.clear()
            self._views[where] = view
            self._views.move_to_end(where)
            while len(self._views) > self.max_views:
                self._views.popitem(last=False)
            return view

    def _full(self, path: str, project: Project, key: Key, order: Callable[[Any], Any] | None) -> View:
        with open(path, "rb") as fh:
            data = fh.read()
        view = View()
        view.offset = parse_into(view, data, project, key, order)
        view.anchor = _sig(data[max(0, view.offset - ANCHOR_BYTES):view.offset])
        self.full_reads += 1
        return view

    def _grow(self, old: View, path: str, size: int, project: Project, key: Key,
              order: Callable[[Any], Any] | None) -> View | None:
        """`old` extended by the bytes after its offset, or None when the file is not `old` plus
        appended bytes (shrunk, or the bytes before the offset changed)."""
        if size < old.offset:
            return None
        lo = max(0, old.offset - ANCHOR_BYTES)
        with open(path, "rb") as fh:
            fh.seek(lo)
            data = fh.read()
        cut = old.offset - lo
        if len(data) < cut or _sig(data[:cut]) != old.anchor:
            return None
        view = View()
        view.groups = {k: list(v) for k, v in old.groups.items()}  # a View handed out never changes
        view.damage = set(old.damage)
        used = parse_into(view, data[cut:], project, key, order)
        view.offset = old.offset + used
        view.anchor = _sig(data[max(0, view.offset - ANCHOR_BYTES - lo):view.offset - lo])
        self.tail_reads += 1
        return view


def _sig(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()
