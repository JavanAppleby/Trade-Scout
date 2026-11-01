"""Logging (DESIGN 2.1): `logs/scout-<ET date>.log` plus the console, every record redacted.

`ANTHROPIC_LOG` is removed from the environment so the SDK never logs requests (SS 2.8).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING

from scout.secrets import redact

if TYPE_CHECKING:  # pragma: no cover
    from scout.config import ScoutConfig
    from scout.paths import ScoutPaths

LOGGER = "scout"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

__all__ = ["LOGGER", "DailyFileHandler", "RedactingFilter", "setup_logging"]


class RedactingFilter(logging.Filter):
    """Applies secrets.redact to every record's final message."""

    def __init__(self, cfg: ScoutConfig | None = None) -> None:
        super().__init__()
        self.cfg = cfg

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break logging
            message = str(record.msg)
        record.msg = redact(message, self.cfg)
        record.args = None
        return True


class DailyFileHandler(logging.FileHandler):
    """`logs/scout-<ET date>.log`, moving to the new date's file at the first record after ET
    midnight (`today()` gives the ET date; a Scout left running all week writes one file a day)."""

    def __init__(self, paths: ScoutPaths, day: date, today: Callable[[], date] | None = None) -> None:
        self.paths, self.day, self.today = paths, day, today
        super().__init__(self._path(day), encoding="utf-8")

    def _path(self, day: date) -> str:
        target = self.paths.writable(self.paths.logs / f"scout-{day.isoformat()}.log")
        target.parent.mkdir(parents=True, exist_ok=True)
        return os.fspath(target)

    def roll(self, day: date) -> None:
        """Move to `day`'s file (no-op when already there)."""
        if day == self.day:
            return
        self.acquire()
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None  # type: ignore[assignment]
            self.baseFilename = os.path.abspath(self._path(day))
            self.day = day
            self.stream = self._open()
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        if self.today is not None:
            try:
                self.roll(self.today())
            except Exception:  # noqa: BLE001 - logging never raises; keep the current file
                pass
        super().emit(record)


def setup_logging(paths: ScoutPaths, level: str, day: date, cfg: ScoutConfig | None = None,
                  today: Callable[[], date] | None = None) -> logging.Logger:
    """Scout's logger: one file per ET date in logs/ (rolled at ET midnight when `today` is given),
    plus the console."""
    os.environ.pop("ANTHROPIC_LOG", None)
    logger = logging.getLogger(LOGGER)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    redacting = RedactingFilter(cfg)
    for handler in (DailyFileHandler(paths, day, today), logging.StreamHandler()):
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(redacting)
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def close_logging() -> None:
    """Close Scout's handlers (so the log file is released on Windows)."""
    logger = logging.getLogger(LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
