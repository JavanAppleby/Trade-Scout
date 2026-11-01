"""Scout's single-instance lock and `python -m scout.running` (DESIGN 2.1; Update Scout asks first).

`ScoutLock` holds an OS lock on `state/scout.lock` (msvcrt on Windows, fcntl elsewhere) for the
life of `python -m scout run`; the OS drops it if the process dies. `main()` probes it without
creating or writing anything: exit 3 with a plain message while Scout runs, else 0.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from scout.paths import ScoutPaths

EXIT_RUNNING = 3
MESSAGE = ("Scout is running. Stop it first (Ctrl+C in the Scout window, then close that window), "
           "then run Update Scout again. Nothing was changed.")

__all__ = ["EXIT_RUNNING", "AlreadyRunning", "ScoutLock", "is_running", "main"]


class AlreadyRunning(RuntimeError):
    """Another Scout holds the lock in this folder."""


class ScoutLock:
    def __init__(self, paths: ScoutPaths) -> None:
        self.path = paths.writable(paths.lock_file)
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the run
        try:
            _lock(fh)
        except OSError:
            fh.close()
            raise AlreadyRunning("another Scout is running in this folder") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            _unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def __enter__(self) -> ScoutLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


if sys.platform == "win32":  # pragma: no cover - platform split
    import msvcrt

    def _lock(fh: IO[str]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: IO[str]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

    def _probe(path: Path) -> bool:
        with open(path, "rb", buffering=0) as fh:
            try:
                fh.read(1)
            except PermissionError:  # the byte a running Scout holds
                return True
        return False
else:  # pragma: no cover - platform split
    import fcntl

    def _lock(fh: IO[str]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: IO[str]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _probe(path: Path) -> bool:
        fd = os.open(path, os.O_RDONLY)  # never O_CREAT
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)


def is_running(folder: Path | str) -> bool:
    """Is a Scout running from `folder`? Read only; a missing lock file means no."""
    try:
        return _probe(Path(folder) / "state" / "scout.lock")
    except OSError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if is_running(Path(args[0]) if args else Path.cwd()):
        print(MESSAGE)
        return EXIT_RUNNING
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
