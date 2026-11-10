"""Scheduler job for the thesis reads (READS_TEST.md 5, 6; owner, 2026-11-10). Nothing is scheduled
unless `reads_enabled` is true: with it false the running Scout tails nothing, calls nothing and
writes nothing (`reads add` still drops a note that waits in inbox\reads).

- `reads`: every `reads_poll_s`, `scout.reads_service.ReadsService.tick` (the owner's notes, then
  Radar's new packs; the Claude calls run on each reader's own worker threads). Without
  `llm_enabled` or the API key only the owner's notes are added.
"""

from __future__ import annotations

from typing import Any

from scout.scheduler import EveryJob, Job

__all__ = ["build_jobs"]


def build_jobs(ctx: Any) -> list[Job]:
    cfg = ctx.cfg
    if not cfg.reads.enabled:
        return []
    from scout.reads_service import ReadsService  # noqa: PLC0415 - only when reads are on

    transport = ctx.llm_transport() if cfg.llm_enabled else None
    if transport is None:
        ctx.log.warning("reads_enabled is true but llm_enabled is false or ANTHROPIC_API_KEY is not set: only the "
                        "owner's read notes are added.")
    service = ReadsService(cfg, ctx.paths, ctx.clock, transport, log=ctx.log.getChild("reads"))
    ctx._services["reads"] = service
    return [EveryJob("reads", service.tick, cfg.reads.poll_s)]
