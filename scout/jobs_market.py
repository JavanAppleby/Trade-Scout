"""Scheduler job for the market feed of the thesis reads (0.3.2; owner, 2026-11-11; SCOUT_SPEC 19.5).

- `market`: every `reads_market_poll_s` (slower after repeated failures: `MarketPoller.interval_at`),
  `scout.market_feed.MarketPoller.tick`, which polls only in the PRE, CORE and POST sessions.

Nothing is scheduled unless `reads_enabled`, `reads_market_enabled`, a `contact_email` and the
owner's Alpaca key (two lines in Scout's .env) are all present. Without the key, one log line names
the accepted line names (never a value). The feed has no board entry and no health record (build
plan amendment B): it is evidence for the reads only.
"""

from __future__ import annotations

from typing import Any

from scout.scheduler import EveryJob, Job
from scout.secrets import ALPACA_ID_NAMES, ALPACA_SECRET_NAMES, alpaca_key

__all__ = ["build_jobs"]


def build_jobs(ctx: Any) -> list[Job]:
    cfg = ctx.cfg
    if not (cfg.reads.enabled and cfg.reads.market_enabled and cfg.http_allowed()):
        return []
    key = alpaca_key()
    if key is None:
        ctx.log.warning("The market feed for the reads is on, but Scout's .env has no Alpaca key: add the key id as "
                        "one of %s and the secret as one of %s. Until then readers see \"Market: not available\" "
                        "unless Radar's pack has the market.", ", ".join(ALPACA_ID_NAMES), ", ".join(ALPACA_SECRET_NAMES))
        return []
    from scout.market_feed import MarketPoller  # noqa: PLC0415 - only when the feed runs

    poller = MarketPoller(cfg, ctx.paths, ctx.fetcher(), key, ctx.clock, raw=ctx.raw(), log=ctx.log.getChild("market"))
    ctx._services["market"] = poller
    return [EveryJob("market", poller.tick, cfg.reads.market_poll_s, interval_fn=poller.interval_at)]
