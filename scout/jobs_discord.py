"""The `discord_inbox` job (DESIGN 2.7, 8): every `inbox_poll_s`, read new Paper Gains Discord capture
files from `inbox/discord/` (`scout.discord_ingest`). At build time the heartbeat's `discord_inbox`
entry is set from the bookkeeping, so a restart shows the true capture status at once."""

from __future__ import annotations

from typing import Any

from scout.discord_ingest import DiscordInbox
from scout.scheduler import EveryJob

__all__ = ["build_jobs", "build_inbox"]


def build_inbox(ctx: Any) -> DiscordInbox:
    return DiscordInbox(ctx.cfg, ctx.paths, ctx.clock, ctx.facts(), ctx.raw(), ctx.health(), ctx.board)


def build_jobs(ctx: Any) -> list[Any]:
    inbox = build_inbox(ctx)
    inbox.publish(ctx.clock.now())
    return [EveryJob("discord_inbox", inbox.poll, ctx.cfg.inbox_poll_s)]
