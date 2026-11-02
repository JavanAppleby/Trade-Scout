"""Shared test safety and fixtures (DESIGN 12).

- no_network (autouse): socket connects only to 127.0.0.1 and name lookups refused. Every attempt is
  recorded and fails the test at teardown, even when the code under test swallowed the error.
- clean_env (autouse): no ANTHROPIC_* variable reaches a test, so no real key.
- clock: a ManualClock at Monday 2026-11-02 14:00Z (09:00 ET, PRE). et(day, "HH:MM") -> aware UTC.
- scout_dir / cfg / paths: a tmp Scout folder (runtime folders created) and a config with a
  placeholder contact email.
- write_audit: an audit-hook recorder that flags every write-like event outside the watched folder.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scout.clock import ManualClock, et_datetime, parse_hhmm
from scout.config import ScoutConfig, config_from_dict
from scout.paths import ScoutPaths, is_within

LOCAL_HOST = "127.0.0.1"
START = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)  # Monday 09:00 ET (EST), a covered trading day
TEST_EMAIL = "test@example.invalid"


class NetworkBlocked(RuntimeError):
    """A test tried to reach a host other than 127.0.0.1."""


def _local(address: object) -> bool:
    if isinstance(address, (str, bytes)):  # an AF_UNIX path
        return True
    host = address[0] if isinstance(address, tuple) and address else address
    return host == LOCAL_HOST


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    attempts: list[str] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def refuse(what: str) -> NetworkBlocked:
        attempts.append(what)
        return NetworkBlocked(f"network access refused in tests: {what}")

    def connect(sock: socket.socket, address: Any) -> Any:
        if not _local(address):
            raise refuse(f"connect {address!r}")
        return real_connect(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> Any:
        if not _local(address):
            raise refuse(f"connect_ex {address!r}")
        return real_connect_ex(sock, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and host != LOCAL_HOST:
            raise refuse(f"getaddrinfo {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
        raise refuse(f"name lookup {host!r}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", lookup)
    monkeypatch.setattr(socket, "gethostbyname_ex", lookup)

    class Guard:
        pass

    guard = Guard()
    guard.attempts = attempts  # type: ignore[attr-defined]
    yield guard
    assert not attempts, f"a test tried to reach the network: {attempts}"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper().startswith("ANTHROPIC_"):
            monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- time


def et_at(day: date | str, hhmm: str) -> datetime:
    """An ET wall time as an aware UTC datetime."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return et_datetime(d, parse_hhmm(hhmm))


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def et() -> Callable[..., datetime]:
    return et_at


# --------------------------------------------------------------------------- folders


@pytest.fixture
def scout_dir(tmp_path: Path) -> Path:
    d = (tmp_path / "Scout").resolve()
    d.mkdir()
    ScoutPaths(d).ensure()
    return d


def make_config(scout_dir: Path, **overrides: object) -> ScoutConfig:
    return config_from_dict({"contact_email": TEST_EMAIL, **overrides}, scout_dir)


@pytest.fixture
def cfg(scout_dir: Path) -> ScoutConfig:
    return make_config(scout_dir)


@pytest.fixture
def paths(cfg: ScoutConfig) -> ScoutPaths:
    return ScoutPaths.from_config(cfg)


# --------------------------------------------------------------------------- write audit

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_PATH_ARGS: dict[str, tuple[int, ...]] = {
    "os.rename": (0, 1), "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "os.truncate": (0,),
    "os.chmod": (0,), "os.symlink": (1,), "os.link": (1,), "os.utime": (0,),
    "shutil.copyfile": (1,), "shutil.copymode": (1,), "shutil.copystat": (1,), "shutil.copytree": (1,),
    "shutil.move": (1,), "shutil.rmtree": (0,),
}


class WriteAudit:
    """Records write-like audit events while active; a path outside every watched root is a
    violation. One instance, one sys.addaudithook for the whole run (Radar's pattern)."""

    def __init__(self) -> None:
        self.active = False
        self.roots: tuple[Path, ...] = ()
        self.events: list[tuple[str, str]] = []
        self.violations: list[tuple[str, str]] = []
        self._busy = threading.local()

    def start(self, root: Path, *also: Path) -> WriteAudit:
        self.roots = tuple(Path(os.path.realpath(r)) for r in (root, *also))
        self.events.clear()
        self.violations.clear()
        self.active = True
        return self

    def stop(self) -> list[tuple[str, str]]:
        self.active = False
        return list(self.violations)

    @contextmanager
    def watching(self, root: Path, *also: Path) -> Iterator[WriteAudit]:
        self.start(root, *also)
        try:
            yield self
        finally:
            self.stop()

    def hook(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.active or getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            for path in self._paths(event, args):
                self._note(event, path)
        except Exception:  # noqa: BLE001 - never break the code under test
            pass
        finally:
            self._busy.on = False

    @staticmethod
    def _paths(event: str, args: tuple[Any, ...]) -> list[Any]:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if isinstance(path, int):
                return []
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                mode is None and isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
            return [path] if writes else []
        idx = _PATH_ARGS.get(event)
        if idx is None:
            return []
        return [args[i] for i in idx if i < len(args) and args[i] is not None and not isinstance(args[i], int)]

    def _note(self, event: str, path: Any) -> None:
        text = os.fsdecode(path) if isinstance(path, (bytes, os.PathLike)) else str(path)
        real = Path(os.path.realpath(os.path.abspath(text)))
        if real.parent.name == "__pycache__" and ".pyc" in real.name:
            return  # the interpreter's own bytecode cache on a first import (a fresh CI checkout), not Scout's write
        self.events.append((event, str(real)))
        if not any(is_within(real, r) for r in self.roots):
            self.violations.append((event, str(real)))


_AUDIT = WriteAudit()
sys.addaudithook(_AUDIT.hook)


@pytest.fixture
def write_audit() -> Iterator[WriteAudit]:
    """The process-wide WriteAudit, reset per test. Call .start(scout_dir) (or use .watching)."""
    _AUDIT.stop()
    _AUDIT.events.clear()
    _AUDIT.violations.clear()
    try:
        yield _AUDIT
    finally:
        violations = _AUDIT.stop()
        assert not violations, f"writes outside Scout's folder: {violations}"
