"""The macro unit's jobs (DESIGN 2.3, 8): halts, nowflation, BLS, the FOMC table and the Cboe VIX.

| Job | When |
|---|---|
| nasdaq_halts | every 60 s (never less; `HARD_MIN_SPACING_S`) inside `active_hours_et`; once after the end of active hours (20:00 ET) on a trading day, the `&haltdate=` read |
| fomc | at start and daily at the first `nowflation_read_times_et` (the table record) |
| bls | when `[sources.bls] min_spacing_s` (weekly) has passed since the last read (checked hourly) |
| nowflation | at start unless already read today; daily at each `nowflation_read_times_et` (08:20 ET); at `nowflation_release_day_time_et` (08:50 ET) on a CPI, NFP or PCE day |
| cboe_vix | at start unless already read since the last scheduled read; trading days at `vix_read_after_et` (16:30 ET) |
| bls_data | at start unless already read today; daily at each `bls_data_read_times_et` (08:40 ET); again at `bls_data_release_day_time_et` (09:15 ET) on a CPI, NFP or PCE day (nowflation's calendar) |

"The last read" comes from the macro history (`MacroWriter.latest`, reloaded at start), so a restart
does not fetch BLS, Cboe or nowflation again (SS 4.1: BLS weekly, Cboe daily, nowflation capped).
The daily jobs are seeded from it too (`_seed`): a restart after 16:30 ET does not read Cboe again
when today's read is in the history, and the same for each nowflation read time. Only a read the
host answered counts (`macro_seed.last_read`): a latest line that failed is read again once at start.
At build time the board is seeded from the same history (`macro_seed.seed_board`), so BLS, Cboe and
nowflation do not sit at "starting" until their next read (see `scout.macro_seed`).

The halts job is checked every `HALTS_CHECK_S` (1 s) and `HaltPoller.ready` keeps the 60 s from the
fetcher's real send time; a 60 s job polled every 120 s (the send lands a few seconds after the tick).

DESIGN lists `macro_merge` "after each of those": every read hands its record to
`MacroWriter.write`, which appends the history line and rewrites the merged daily file at once, so
the merge runs after each read without a job of its own. A disabled source gets no job.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from scout.bls_data import SOURCE as BLS_DATA, BlsDataReader
from scout.bls_ics import BlsReader
from scout.calendar import in_active_hours, is_trading_day, previous_trading_day
from scout.cboe_vix import VixReader
from scout.clock import et_date, et_datetime, parse_iso, to_et
from scout.fomc import fomc_record
from scout.halts import SOURCE as HALTS, HaltPoller
from scout.macro import MacroWriter
from scout.macro_seed import last_read, seed_board
from scout.nowflation import NowflationReader, is_release_day
from scout.scheduler import DailyJob, EveryJob, Job, OnceAtStartJob
from scout.store import read_jsonl

__all__ = ["build_jobs", "halts_tick", "last_fetched", "last_read", "vix_slot"]

BLS_CHECK_S = 3600.0
HALTS_CHECK_S = 1.0  # the job only asks; HaltPoller.ready keeps the 60 s from the real send time


def last_fetched(writer: Any, source: str) -> datetime | None:
    """When `source` was last fetched, from the macro history, whatever the result (`last_read`
    counts only a read the host answered)."""
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


def _seed(job: DailyJob, last: datetime | None) -> DailyJob:
    """Mark `job` done for the ET day of `last` when that read was at or after the job's time that
    day: `DailyJob.last_day` lives in memory only, so without this a restart after the daily time
    would fetch again (SS 4.1: Cboe daily, nowflation capped). `last` comes from `last_read`, so a
    failed read never marks the job done."""
    if last is not None and last >= job.at_on(et_date(last)):
        job.last_day = et_date(last)
    return job


def _enabled(cfg: Any, name: str) -> bool:
    src = cfg.sources.get(name)
    return bool(src is not None and src.enabled)


def build_jobs(ctx: Any) -> list[Job]:
    cfg, clock = ctx.cfg, ctx.clock
    jobs: list[Job] = []
    writer = MacroWriter(ctx.paths, ctx.facts(), cfg, clock)
    readers: dict[str, Any] = {}

    def save(record: Any) -> None:
        if record is not None:
            writer.write(record)

    if _enabled(cfg, HALTS):
        poller = HaltPoller(cfg, ctx.fetcher(), ctx.raw(), ctx.facts(), ctx.health(), ctx.board, clock)
        # Checked every HALTS_CHECK_S, not every 60 s: the request goes out a few seconds after the
        # tick (earlier jobs run first), so a 60 s job would find ready() a few seconds short and
        # wait a whole further interval, polling every 120 s.
        jobs.append(EveryJob("nasdaq_halts", lambda now: halts_tick(poller, cfg, now), interval_s=HALTS_CHECK_S))

    first_read = cfg.nowflation_read_times_et[0]

    def fomc(now: datetime) -> None:
        save(fomc_record(cfg, now))

    jobs.append(OnceAtStartJob("fomc_start", fomc))
    jobs.append(DailyJob("fomc", fomc, first_read, trading_days_only=False))

    if _enabled(cfg, "bls"):
        bls = readers["bls"] = BlsReader(cfg, ctx.fetcher(), ctx.raw(), ctx.health(), ctx.board, clock)
        bls_spacing = timedelta(seconds=cfg.source("bls").min_spacing_s)
        bls_tried: list[datetime] = []

        def bls_read(now: datetime) -> None:
            last = max([t for t in (last_read(writer, "bls"), *bls_tried[-1:]) if t is not None], default=None)
            if last is not None and now - last < bls_spacing:
                return
            bls_tried.append(now)
            del bls_tried[:-1]
            save(bls.read(now))

        jobs.append(EveryJob("bls", bls_read, interval_s=min(BLS_CHECK_S, bls_spacing.total_seconds())))

    if _enabled(cfg, "nowflation"):
        nf = readers["nowflation"] = NowflationReader(cfg, ctx.fetcher(), ctx.raw(), ctx.health(), ctx.board, clock)

        def nowflation(now: datetime) -> None:
            save(nf.read(now))

        def release_day(now: datetime) -> None:
            if is_release_day(nf.last_events, et_date(now)) and nf.last_read_at != now:
                save(nf.read(now))

        nf_last = last_read(writer, "nowflation")
        daily = [_seed(DailyJob(f"nowflation@{t:%H:%M}", nowflation, t, trading_days_only=False), nf_last)
                 for t in cfg.nowflation_read_times_et]
        def nowflation_start(now: datetime) -> None:
            last = last_read(writer, "nowflation")
            if last is None or et_date(last) != et_date(now):
                nowflation(now)

        jobs.append(OnceAtStartJob("nowflation_start", _unless_due(daily, nowflation_start)))
        jobs.extend(daily)
        jobs.append(_seed(DailyJob("nowflation_release_day", release_day, cfg.nowflation_release_day_time_et,
                                   trading_days_only=False), nf_last))

    if _enabled(cfg, "cboe_vix"):
        vix = readers["cboe_vix"] = VixReader(cfg, ctx.fetcher(), ctx.raw(), ctx.health(), ctx.board, clock)
        vix_daily = _seed(DailyJob("cboe_vix", lambda now: save(vix.read(now)), cfg.vix_read_after_et,
                                   trading_days_only=True), last_read(writer, "cboe_vix"))
        def vix_start(now: datetime) -> None:
            last = last_read(writer, "cboe_vix")
            if last is None or last < vix_slot(cfg, now):
                save(vix.read(now))

        jobs.append(OnceAtStartJob("cboe_vix_start", _unless_due([vix_daily], vix_start)))
        jobs.append(vix_daily)

    if _enabled(cfg, BLS_DATA):
        history_path = ctx.paths.macro / "history.jsonl"
        bd = readers[BLS_DATA] = BlsDataReader(cfg, ctx.fetcher(), ctx.raw(), ctx.health(), ctx.board, clock,
                                               history=lambda: read_jsonl(history_path))

        def bls_data(now: datetime) -> None:
            save(bd.read(now))

        def bls_data_release(now: datetime) -> None:
            # a second read on release days, in case BLS posted after the morning read (3 more requests)
            nf_reader = readers.get("nowflation")
            if nf_reader is not None and is_release_day(nf_reader.last_events, et_date(now)):
                bls_data(now)

        bd_last = last_read(writer, BLS_DATA)
        bd_daily = [_seed(DailyJob(f"bls_data@{t:%H:%M}", bls_data, t, trading_days_only=False), bd_last)
                    for t in cfg.bls_data_read_times_et]

        def bls_data_start(now: datetime) -> None:
            last = last_read(writer, BLS_DATA)
            if last is None or et_date(last) != et_date(now):
                bls_data(now)

        jobs.append(OnceAtStartJob("bls_data_start", _unless_due(bd_daily, bls_data_start)))
        jobs.extend(bd_daily)
        jobs.append(_seed(DailyJob("bls_data_release_day", bls_data_release, cfg.bls_data_release_day_time_et,
                                   trading_days_only=False), bd_last))
    seed_board(ctx.board, cfg, writer, clock.now(), readers)
    return jobs
