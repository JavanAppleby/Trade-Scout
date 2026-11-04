"""Layer-1 EDGAR and wire jobs (DESIGN 2.2, 8).

- `edgar_tickers`: checked every minute; reads the CIK map at start and once per ET day from 03:50
  ET (a failed read is tried again after 10 minutes).
- `edgar_atom`: every `min_spacing_s` (60 s) 04:00-20:00 ET, `idle_spacing_s` (600 s) otherwise.
- every source of kind "wire" (`prnewswire`, `stocktitan`): every `min_spacing_s` (60 s), `idle_spacing_s`
  outside 04:00-20:00 ET; a source in `hour_turn_sources` (PR Newswire) every `hour_turn_spacing_s`
  (30 s) from :58 to :05 ET around each hour inside those hours (`scout.health.spacing_in_force`;
  owner, 2026-11-04).
- the EDGAR cycle runs the due wire jobs between its form requests (`EdgarAtomPoller.between`;
  owner, 2026-11-04): one thread, so the fetcher's host spacing, the writers and the heartbeat are
  used exactly as before, and a slow SEC no longer pushes the wire polls past `source_max_gap_s`.
A disabled source gets no job (the board shows it `disabled`). One CoverageTracker serves every
source of this module, seeded from Scout's own stored facts and health records (`scout.coverage_seed`,
owner 2026-11-04) so a restart does not open a coverage gap by itself.
"""

from __future__ import annotations

from typing import Any

from scout.coverage_seed import load_seeds
from scout.edgar_atom import SOURCE as EDGAR_ATOM
from scout.edgar_atom import EdgarAtomPoller
from scout.health import CoverageTracker, board_state, spacing_in_force
from scout.scheduler import EveryJob, Job, run_due
from scout.wire_rss import WirePoller

__all__ = ["build_jobs", "wire_sources"]

TICKER_CHECK_S = 60.0


def wire_sources(cfg: Any) -> list[str]:
    """The configured RSS wires, in table order (GlobeNewswire was removed on 2026-11-03: it refuses
    automated readers)."""
    return [name for name, src in cfg.sources.items() if src.kind == "wire"]


def build_jobs(ctx: Any) -> list[Job]:
    cfg = ctx.cfg
    wires = wire_sources(cfg)
    seeds = load_seeds(ctx.paths, ctx.clock.now(), sources=(EDGAR_ATOM, *wires), cap=cfg.seen_guid_cap,
                       days=cfg.fact_dedupe_days)
    tracker = CoverageTracker(cfg, seeds)
    jobs: list[Job] = []
    for name in ("edgar_tickers", EDGAR_ATOM, *wires):
        src = cfg.source(name)
        if not src.enabled:
            ctx.board.update(name, board_state(False, "disabled", None, None, False))
    ticker_map = ctx.ticker_map()
    if cfg.source("edgar_tickers").enabled:
        jobs.append(EveryJob("edgar_tickers", ticker_map.ensure, TICKER_CHECK_S))
    elif hasattr(ticker_map, "refresh"):
        ticker_map.refresh(ctx.clock.now())  # disabled: load the last stored copy, if any
    wire_jobs: list[Job] = []
    for name in wires:
        src = cfg.source(name)
        if not src.enabled:
            continue
        wire = WirePoller(cfg, name, ctx.fetcher(), ctx.raw(), ctx.facts(), ctx.health(), ctx.board, ctx.clock,
                          tracker=tracker, log=ctx.log.getChild(name))
        wire_jobs.append(EveryJob(name, wire.poll, src.min_spacing_s, src.idle_spacing_s or None, cfg,
                                  interval_fn=lambda now, s=src: spacing_in_force(s, now, cfg)))
    if cfg.source(EDGAR_ATOM).enabled:
        src = cfg.source(EDGAR_ATOM)
        log = ctx.log.getChild(EDGAR_ATOM)

        def between() -> None:  # the same job objects as the main loop's: a wire run here is not run twice
            run_due(wire_jobs, ctx.clock.now(), log, ctx.clock)

        poller = EdgarAtomPoller(cfg, ctx.fetcher(), ctx.raw(), ctx.facts(), ctx.health(), ctx.board, ticker_map,
                                 ctx.clock, tracker=tracker, log=log, between=between if wire_jobs else None)
        jobs.append(EveryJob(EDGAR_ATOM, poller.poll, src.min_spacing_s, src.idle_spacing_s or None, cfg))
    return jobs + wire_jobs
