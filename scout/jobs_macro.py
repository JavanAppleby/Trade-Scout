"""The macro unit's jobs (DESIGN 2.3, 8): halts, nowflation, BLS, the FOMC table and the Cboe VIX.

| Job | When |
|---|---|
| nasdaq_halts | every 60 s (never less; `HARD_MIN_SPACING_S`) inside `active_hours_et`; once after the end of active hours (20:00 ET) on a trading day, the `&haltdate=` read |
| fomc | at start and daily at the first `nowflation_read_times_et` (the table record) |
| bls | when `[sources.bls] min_spacing_s` (weekly) has passed since the last read (checked hourly) |
| nowflation | at start unless already read today; daily at each `nowflation_read_times_et` (08:20 ET); at `nowflation_release_day_time_et` (08:50 ET) on a CPI, NFP or PCE day |
| cboe_vix | at start unless already read since the last scheduled read; trading days at `vix_read_after_et` (16:30 ET) |

"The last read" comes from the macro history (`MacroWriter.latest`, reloaded at start), so a restart
does not fetch BLS, Cboe or nowflation again (SS 4.1: BLS weekly, Cboe daily, nowflation capped).

DESIGN lists `macro_merge` "after each of those": every read hands its record to
`MacroWriter.write`, which appends the history line and rewrites the merged daily file at once, so
the merge runs after each read without a job of its own. A disabled source gets no job.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from scout.bls_ics import BlsReader
from scout.calendar import in_active_hours, is_trading_day, previous_trading_day
from scout.cboe_vix import VixReader
from scout.clock import et_date, et_datetime, parse_iso, to_et
from scout.fomc import fomc_record
from scout.halts import SOURCE as HALTS, HaltPoller
from scout.macro import MacroWriter
from scout.nowflation import NowflationReader, is_release_day
from scout.scheduler import DailyJob, EveryJob, Job, OnceAtStartJob

__all__ = ["build_jobs", "halts_tick", "last_fetched", "vix_slot"]

BLS_CHECK_S = 3600.0


def last_fetched(writer: Any, source: str) -> datetime | None:
    """When `source` was last read, from the macro history (survives a restart)."""
    rec = (getattr(writer, "latest", None) or {}).get(source)
    if not isinstance(rec, dict):
        return None
    return parse_iso(rec.get("fetched_at")) or parse_iso(rec.get("first_seen_at"))


def vix_slot(cfg: Any, now: datetime) -> datetime:
    """The latest scheduled VIX read time at or before `now` (a trading day at vix_read_after_et)."""
    day = et_date(now)
    if not is_trading_day(day) or et_datetime(day, cfg.vix_read_after_et) > now:
        day = previous_trading_day(day)
    return et_datetime(day, cfg.vix_read_after_et)


def halts_tick(poller: HaltPoller, cfg: Any, now: datetime) -> int:
    """Inside active hours: the live poll. After them on a trading day: the one dated read."""
    if in_active_hours(now, cfg):
        return poller.poll(now)
    end = cfg.active_hours_et[1]
    if to_et(now).time() >= end and is_trading_day(et_date(now)):
        return poller.dated_read(now)
    return 0


def _unless_due(daily: list[DailyJob], fn: Any) -> Any:
    """A start read that steps aside when a daily read is due on the same tick (no double read)."""

    def run(now: datetime) -> None:
        if not any(j.due(now) for j in daily):
            fn(now)

    return run


def _enabled(cfg: Any, name: str) -> bool:
    src = cfg.sources.get(name)
    return bool(src is not None and src.enabled)


def build_jobs(ctx: Any) -> list[Job]:
    cfg, clock = ctx.cfg, ctx.clock
    jobs: list[Job] = []
    writer = MacroWriter(ctx.paths, ctx.facts(), cfg, clock)

    def save(record: Any) -> None:
        if record is not None:
            writer.write(record)

    if _enabled(cfg, HALTS):
        poller = HaltPoller(cfg, ctx.fetcher(), ctx.raw(), ctx.facts(), ctx.health(), ctx.board, clock)
        jobs.append(EveryJob("nasdaq_halts", lambda now: halts_tick(poller, cfg, now), interval_s=poller.spacing_s))

    first_read = cfg.nowflation_read_times_et[0]

    def fomc(now: datetime) -> None:
        save(fomc_record(cfg, now))

    jobs.append(OnceAtStartJob("fomc_start", fomc))
    jobs.append(DailyJob("fomc", fomc, first_read, trading_days_only=False))

    if _enabled(cfg, "bls"):
        bls = BlsReader(cfg, ctx.fetcher(), ctx.raw(), ctx.health(), ctx.board, clock)
        bls_spacing = timedelta(seconds=cfg.source("bls").min_spacing_s)
        bls_tried: list[datetime] = []

        def bls_read(now: datetime) -> None:
            last = max([t for t in (last_fetched(writer, "bls"), *bls_tried[-1:]) if t is not None], default=None)
            if last is not None and now - last < bls_spacing:
                return
            bls_tried.append(now)
            del bls_tried[:-1]
            save(bls.read(now))

        jobs.append(EveryJob("bls", bls_read, interval_s=min(BLS_CHECK_S, bls_spacing.total_seconds())))

    if _enabled(cfg, "nowflation"):
        nf = NowflationReader(cfg, ctx.fetcher(), ctx.raw(), ctx.health(), ctx.board, clock)

        def nowflation(now: datetime) -> None:
            save(nf.read(now))

        def release_day(now: datetime) -> None:
            if is_release_day(nf.last_events, et_date(now)) and nf.last_read_at != now:
                save(nf.read(now))

        daily = [DailyJob(f"nowflation@{t:%H:%M}", nowflation, t, trading_days_only=False)
                 for t in cfg.nowflation_read_times_et]
        def nowflation_start(now: datetime) -> None:
            last = last_fetched(writer, "nowflation")
            if last is None or et_date(last) != et_date(now):
                nowflation(now)

        jobs.append(OnceAtStartJob("nowflation_start", _unless_due(daily, nowflation_start)))
        jobs.extend(daily)
        jobs.append(DailyJob("nowflation_release_day", release_day, cfg.nowflation_release_day_time_et,
                             trading_days_only=False))

    if _enabled(cfg, "cboe_vix"):
        vix = VixReader(cfg, ctx.fetcher(), ctx.raw(), ctx.health(), ctx.board, clock)
        vix_daily = DailyJob("cboe_vix", lambda now: save(vix.read(now)), cfg.vix_read_after_et, trading_days_only=True)
        def vix_start(now: datetime) -> None:
            last = last_fetched(writer, "cboe_vix")
            if last is None or last < vix_slot(cfg, now):
                save(vix.read(now))

        jobs.append(OnceAtStartJob("cboe_vix_start", _unless_due([vix_daily], vix_start)))
        jobs.append(vix_daily)
    return jobs
