"""Layer-1 EDGAR and wire jobs (DESIGN 2.2, 8).

- `edgar_tickers`: checked every minute; reads the CIK map at start and once per ET day from 03:50
  ET (a failed read is tried again after 10 minutes).
- `edgar_atom`: every `min_spacing_s` (60 s) 04:00-20:00 ET, `idle_spacing_s` (600 s) otherwise.
- every source of kind "wire" (`prnewswire`, `stocktitan`): every `min_spacing_s` (60 s), `idle_spacing_s`
  outside 04:00-20:00 ET.
A disabled source gets no job (the board shows it `disabled`). One CoverageTracker serves every
source of this module.
"""

from __future__ import annotations

from typing import Any

from scout.edgar_atom import SOURCE as EDGAR_ATOM
from scout.edgar_atom import EdgarAtomPoller
from scout.health import CoverageTracker, board_state
from scout.scheduler import EveryJob, Job
from scout.wire_rss import WirePoller

__all__ = ["build_jobs", "wire_sources"]

TICKER_CHECK_S = 60.0


def wire_sources(cfg: Any) -> list[str]:
    """The configured RSS wires, in table order (GlobeNewswire was removed on 2026-11-03: it refuses
    automated readers)."""
    return [name for name, src in cfg.sources.items() if src.kind == "wire"]


def build_jobs(ctx: Any) -> list[Job]:
    cfg = ctx.cfg
    tracker = CoverageTracker(cfg)
    jobs: list[Job] = []
    wires = wire_sources(cfg)
    for name in ("edgar_tickers", EDGAR_ATOM, *wires):
        src = cfg.source(name)
        if not src.enabled:
            ctx.board.update(name, board_state(False, "disabled", None, None, False))
    ticker_map = ctx.ticker_map()
    if cfg.source("edgar_tickers").enabled:
        jobs.append(EveryJob("edgar_tickers", ticker_map.ensure, TICKER_CHECK_S))
    elif hasattr(ticker_map, "refresh"):
        ticker_map.refresh(ctx.clock.now())  # disabled: load the last stored copy, if any
    if cfg.source(EDGAR_ATOM).enabled:
        src = cfg.source(EDGAR_ATOM)
        poller = EdgarAtomPoller(cfg, ctx.fetcher(), ctx.raw(), ctx.facts(), ctx.health(), ctx.board, ticker_map,
                                 ctx.clock, tracker=tracker, log=ctx.log.getChild(EDGAR_ATOM))
        jobs.append(EveryJob(EDGAR_ATOM, poller.poll, src.min_spacing_s, src.idle_spacing_s or None, cfg))
    for name in wires:
        src = cfg.source(name)
        if not src.enabled:
            continue
        wire = WirePoller(cfg, name, ctx.fetcher(), ctx.raw(), ctx.facts(), ctx.health(), ctx.board, ctx.clock,
                          tracker=tracker, log=ctx.log.getChild(name))
        jobs.append(EveryJob(name, wire.poll, src.min_spacing_s, src.idle_spacing_s or None, cfg))
    return jobs
