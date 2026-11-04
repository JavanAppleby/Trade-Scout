"""The job model (DESIGN 2.1, 8). Each job says when it is due; `run_due` runs the due ones, each
inside its own try/except, so one failing job never stops the others or the heartbeat."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol, runtime_checkable

from scout.calendar import in_active_hours, is_early_close, is_trading_day
from scout.clock import et_date, et_datetime
from scout.config import ScoutConfig
from scout.secrets import redact

JobFn = Callable[[datetime], object]
#: A last run more than this after `now` means the wall clock stepped back: the job is due.
CLOCK_STEP_BACK_S = 5.0

__all__ = ["DailyJob", "EveryJob", "Job", "OnceAtStartJob", "run_due"]


@runtime_checkable
class Job(Protocol):
    name: str

    def due(self, now: datetime) -> bool: ...

    def run(self, now: datetime) -> None: ...


class EveryJob:
    """Every `interval_s`; `idle_interval_s` (when given with `cfg`) outside `active_hours_et`;
    `interval_fn(now)`, when given, decides instead (the wires: `scout.health.spacing_in_force`, so a
    job runs at the spacing its health records store). Due at once on the first tick."""

    def __init__(self, name: str, fn: JobFn, interval_s: float, idle_interval_s: float | None = None,
                 cfg: ScoutConfig | None = None, interval_fn: Callable[[datetime], float] | None = None) -> None:
        self.name, self.fn = name, fn
        self.interval_s, self.idle_interval_s, self.cfg = float(interval_s), idle_interval_s, cfg
        self.interval_fn = interval_fn
        self.last_run: datetime | None = None

    def interval_at(self, now: datetime) -> float:
        if self.interval_fn is not None:
            return float(self.interval_fn(now))
        if self.idle_interval_s and self.cfg is not None and not in_active_hours(now, self.cfg):
            return float(self.idle_interval_s)
        return self.interval_s

    def due(self, now: datetime) -> bool:
        if self.last_run is None:
            return True
        elapsed = (now - self.last_run).total_seconds()
        interval = self.interval_at(now)
        if elapsed < 0:
            # A last run a few seconds after `now` (a job run inside another job, e.g. the wires
            # between EDGAR's forms, judged by a caller without the clock): not due again. A wall
            # clock that stepped back further makes it due at once, as before 0.2.2, so a job never
            # waits out a clock step.
            return -elapsed > CLOCK_STEP_BACK_S
        return elapsed >= interval - 1e-6

    def run(self, now: datetime) -> None:
        self.last_run = now
        self.fn(now)


class DailyJob:
    """Once per ET date at `at_et` (earlier by `early_close_shift_h` on early-close days), on trading
    days only unless told otherwise. Started after the time, it runs once that day."""

    def __init__(self, name: str, fn: JobFn, at_et: time, trading_days_only: bool = True,
                 early_close_shift_h: float = 0.0) -> None:
        self.name, self.fn, self.at_et = name, fn, at_et
        self.trading_days_only, self.early_close_shift_h = trading_days_only, float(early_close_shift_h)
        self.last_day: date | None = None

    def at_on(self, day: date) -> datetime:
        when = et_datetime(day, self.at_et)
        if self.early_close_shift_h and is_early_close(day):
            when -= timedelta(hours=self.early_close_shift_h)
        return when

    def due(self, now: datetime) -> bool:
        day = et_date(now)
        if self.last_day == day:
            return False
        if self.trading_days_only and not is_trading_day(day):
            return False
        return now >= self.at_on(day)

    def run(self, now: datetime) -> None:
        self.last_day = et_date(now)
        self.fn(now)


class OnceAtStartJob:
    def __init__(self, name: str, fn: JobFn) -> None:
        self.name, self.fn = name, fn
        self.done = False

    def due(self, now: datetime) -> bool:
        return not self.done

    def run(self, now: datetime) -> None:
        self.done = True
        self.fn(now)


def run_due(jobs: Iterable[Job], now: datetime, log: logging.Logger, clock: Any = None) -> list[str]:
    """Run every due job; returns the names run (failed ones included). A failure is logged with the
    job name, the exception type and a redacted message. Given `clock`, each job is asked and run at
    the clock's time when its turn comes (0.2.2: after a long EDGAR cycle, which ran the wires in
    between, the wires are judged at the real time, not at the tick's start)."""
    ran: list[str] = []
    for job in jobs:
        if clock is not None:
            now = clock.now()
        try:
            if not job.due(now):
                continue
        except Exception as exc:  # noqa: BLE001
            log.error("job %s: due() failed: %s: %s", job.name, type(exc).__name__, redact(str(exc)))
            continue
        ran.append(job.name)
        try:
            job.run(now)
        except Exception as exc:  # noqa: BLE001 - one failing job never stops the others
            log.error("job %s failed: %s: %s", job.name, type(exc).__name__, redact(str(exc)))
    return ran
