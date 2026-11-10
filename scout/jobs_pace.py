"""The polling pace jobs (owner, 2026-11-10; DESIGN 21; `scout.pace`).

| Job | When |
|---|---|
| pace_start | at start: recalculate when the saved plan is missing, more than a day old or made with other `[pace]` settings (unless the daily time is due on the same tick) |
| pace | every day at `pace.recalc_time_et` (03:30 ET), closed days included |

First in `scout.app.JOB_MODULES`, so a recalculation at start runs before the first polls. The `Pace`
object itself is shared through `AppContext.pace()`: the wire and EDGAR schedules, the coverage
tracker and the heartbeat ask it for the spacing in force. No jobs when `pace.enabled` is false.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from scout.clock import et_date
from scout.scheduler import DailyJob, Job, OnceAtStartJob

__all__ = ["build_jobs"]


def build_jobs(ctx: Any) -> list[Job]:
    cfg = ctx.cfg
    if not cfg.pace.enabled:
        return []
    pace = ctx.pace()

    def recalc(now: datetime) -> None:
        pace.recalc(now)
        if now >= daily.at_on(et_date(now)):
            daily.last_day = et_date(now)  # done for today: a start after 03:30 does not read twice

    daily = DailyJob("pace", recalc, cfg.pace.recalc_time_et, trading_days_only=False)
    plan = pace.plan
    if plan is not None and plan.made_at >= daily.at_on(et_date(plan.made_at)):
        daily.last_day = et_date(plan.made_at)  # DailyJob keeps its day in memory only

    def start(now: datetime) -> None:
        if not daily.due(now) and pace.stale(now):
            recalc(now)

    return [OnceAtStartJob("pace_start", start), daily]
