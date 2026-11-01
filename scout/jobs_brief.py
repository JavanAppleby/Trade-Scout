"""Scheduler jobs for the brief, verification and the ledger status (DESIGN 2.6, 8).

- `brief`: at `brief_time_et` on trading days, writes `reports/scout-<ET date>.md` (J4 inside when on).
- `verify`: `verify_time_et` (20:30) on trading days (3 hours earlier on early closes): picks the week's sample once the
  `verify_sample_weekday` has come, then writes the verification report over every answered day.
- `ledger_status`: every minute, publishes LLM spend and the cap state to the heartbeat board.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from scout.brief import write_brief
from scout.brief_sections import iso_week
from scout.clock import et_date
from scout.config import WEEKDAYS
from scout.llm_ledger import Ledger
from scout.scheduler import DailyJob, EveryJob, Job
from scout.verify import first_answer_day, weekly_sample, write_report

EARLY_CLOSE_SHIFT_H = 3.0
LEDGER_STATUS_S = 60

__all__ = ["build_jobs"]


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
        DailyJob("brief", brief, cfg.brief_time_et),
        DailyJob("verify", verify, cfg.verify_time_et, early_close_shift_h=EARLY_CLOSE_SHIFT_H),
        EveryJob("ledger_status", ledger_status, LEDGER_STATUS_S),
    ]
