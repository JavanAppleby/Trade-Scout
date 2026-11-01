"""The answers unit's jobs (DESIGN 2.5, 8): `answers_pilot_HHMM` at each `answer_times_et` (19:30 and
19:45 ET) and `answers_radar` after `radar_answers_after_et` (20:00 ET), trading days only, every
time 3 hours earlier on an early close.

Each waits for the main loop's first live beat (`ctx.live_beat_at`): Scout started or restarted
after 19:30 ET would otherwise answer on its first tick, when the only beat is the "starting" one,
write every answer stale and unknown, and mark the job done for the day."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from scout.answers import EARLY_CLOSE_SHIFT_H, AnswersJob
from scout.models import PeriodicInfo
from scout.scheduler import DailyJob, Job

__all__ = ["AfterLiveBeat", "LazySubmissions", "build_jobs"]


class AfterLiveBeat:
    """A job that is due only once the main loop has written a live beat at or before `now`. A
    context without `live_beat_at` (tests that drive jobs by hand) never waits."""

    def __init__(self, job: DailyJob, ctx: Any) -> None:
        self.job, self.ctx, self.name = job, ctx, job.name

    def due(self, now: datetime) -> bool:
        if not self.job.due(now):
            return False
        if not hasattr(self.ctx, "live_beat_at"):
            return True
        at = self.ctx.live_beat_at
        return at is not None and at <= now

    def run(self, now: datetime) -> None:
        self.job.run(now)

    def __getattr__(self, name: str) -> Any:  # at_on, at_et, last_day ... come from the daily job
        if name in ("job", "ctx", "name"):
            raise AttributeError(name)
        return getattr(self.job, name)


class LazySubmissions:
    """implements SubmissionsSource: resolves `ctx.submissions()` on first use, so the jobs load
    before the wire unit's module exists; when it cannot be built every answer's due record says
    `fetch_failed` (gate 5 unknown)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def periodic_info(self, ticker: str, now: datetime) -> PeriodicInfo:
        try:
            source = self.ctx.submissions()
        except Exception as exc:  # noqa: BLE001 - fail closed, never stop the job
            self.ctx.log.warning("answers: EDGAR submissions not available (%s)", type(exc).__name__)
            return PeriodicInfo(ticker=ticker.upper(), cik=None, status="fetch_failed")
        return source.periodic_info(ticker, now)


def build_jobs(ctx: Any) -> list[Job]:
    job = AnswersJob(ctx.cfg, ctx.paths, LazySubmissions(ctx), ctx.clock, ctx.log)
    daily = [
        DailyJob(f"answers_pilot_{t.strftime('%H%M')}", job.run_pilot, t, early_close_shift_h=EARLY_CLOSE_SHIFT_H)
        for t in ctx.cfg.answer_times_et
    ]
    daily.append(DailyJob("answers_radar", job.run_radar, ctx.cfg.radar_answers_after_et,
                          early_close_shift_h=EARLY_CLOSE_SHIFT_H))
    jobs: list[Job] = [AfterLiveBeat(d, ctx) for d in daily]
    return jobs
