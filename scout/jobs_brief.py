"""Scheduler jobs for the brief, verification and the ledger status (DESIGN 2.6, 8).

- `brief`: at `brief_time_et` on trading days, writes `reports/scout-<ET date>.md` (J4 inside when on).
  It waits for the main loop's first live beat (`AfterLiveBeat`) and is seeded from today's brief
  file (`_seed_brief`): a start or restart after 08:20 ET otherwise rewrote a good brief on the first
  tick, from the "starting" heartbeat (STALE, no source lines), and paid for J4 again (third review).
- `verify`: `verify_time_et` (20:30) on trading days (3 hours earlier on early closes): picks the week's sample once the
  `verify_sample_weekday` has come, then writes the verification report over every answered day.
- `ledger_status`: every minute, publishes LLM spend and the cap state to the heartbeat board.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from scout.brief import brief_path, write_brief
from scout.brief_sections import iso_week
from scout.clock import et_date
from scout.config import WEEKDAYS
from scout.jobs_answers import AfterLiveBeat
from scout.llm_ledger import Ledger
from scout.scheduler import DailyJob, EveryJob, Job
from scout.verify import first_answer_day, weekly_sample, write_report

EARLY_CLOSE_SHIFT_H = 3.0
LEDGER_STATUS_S = 60

__all__ = ["build_jobs"]


def _seed_brief(job: DailyJob, ctx: Any) -> DailyJob:
    """Mark the brief done for today when today's brief file was written at or after today's brief
    time (`DailyJob.last_day` lives in memory only). A brief written earlier by hand (`Scout Brief`
    at 06:00) does not count, so the 08:20 brief still runs."""
    today = et_date(ctx.clock.now())
    try:
        written = datetime.fromtimestamp(brief_path(ctx, today).stat().st_mtime, timezone.utc)
    except (OSError, ValueError, OverflowError):
        return job
    if written >= job.at_on(today):
        job.last_day = today
    return job


def build_jobs(ctx: Any) -> list[Job]:
    cfg, paths = ctx.cfg, ctx.paths
    ledger = Ledger(paths, cfg, ctx.clock)

    def brief(now: datetime) -> None:
        # as of the moment it is written, so reads made earlier in the same tick (the 08:20
        # nowflation read) are in it
        write_brief(ctx, et_date(now), max(now, ctx.clock.now()))

    def verify(now: datetime) -> None:
        today = et_date(now)
        if today.weekday() >= WEEKDAYS.index(cfg.verify_sample_weekday):
            weekly_sample(paths, iso_week(today), None, now=now, size=cfg.verify_sample_size)
        write_report(paths, first_answer_day(paths) or today, today, cfg=cfg, now=now)

    def ledger_status(now: datetime) -> None:
        ctx.board.set_llm(cfg.llm_enabled, ledger.spent_today() if cfg.llm_enabled else 0.0,
                          ledger.cap_reached() if cfg.llm_enabled else False)

    return [
        AfterLiveBeat(_seed_brief(DailyJob("brief", brief, cfg.brief_time_et), ctx), ctx),
        DailyJob("verify", verify, cfg.verify_time_et, early_close_shift_h=EARLY_CLOSE_SHIFT_H),
        EveryJob("ledger_status", ledger_status, LEDGER_STATUS_S),
    ]
