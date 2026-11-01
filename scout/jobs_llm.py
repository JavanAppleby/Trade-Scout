"""Scheduler jobs for the Claude API (DESIGN 2.6, 8). Nothing is scheduled unless `llm_enabled`.

- `llm_j1`: every `j1_interval_s`; the job itself runs only in PRE/CORE/POST.
- `llm_j3`: daily at `j3_time_et` (3 hours earlier on early-close days) for open Pilot positions.
- `llm_j3_gap`: every minute, the Radar finds with no Layer-1 fact `j3_no_fact_after_s` after the find.
J4 runs inside the brief job (`scout.jobs_brief`).
"""

from __future__ import annotations

from typing import Any

from scout.llm_j1 import J1Job
from scout.llm_j3 import J3Job
from scout.llm_ledger import Ledger
from scout.scheduler import DailyJob, EveryJob, Job

GAP_CHECK_S = 60
EARLY_CLOSE_SHIFT_H = 3.0

__all__ = ["build_jobs"]


def build_jobs(ctx: Any) -> list[Job]:
    cfg = ctx.cfg
    if not cfg.llm_enabled:
        return []
    transport = ctx.llm_transport()
    if transport is None:
        ctx.log.warning("llm_enabled is true but ANTHROPIC_API_KEY is not set: the Claude jobs do not run.")
        return []
    ledger = Ledger(ctx.paths, cfg, ctx.clock)
    jobs: list[Job] = []
    if cfg.llm_job("J1").enabled:
        j1 = J1Job(cfg, ctx.paths, transport, ledger, ctx.clock, log=ctx.log)
        jobs.append(EveryJob("llm_j1", j1.run, cfg.j1_interval_s))
    if cfg.llm_job("J3").enabled:
        j3 = J3Job(cfg, ctx.paths, transport, ledger, ctx.clock, log=ctx.log)
        jobs.append(DailyJob("llm_j3", j3.run_positions, cfg.j3_time_et, early_close_shift_h=EARLY_CLOSE_SHIFT_H))
        jobs.append(EveryJob("llm_j3_gap", j3.run_gap_fill, GAP_CHECK_S))
    return jobs
