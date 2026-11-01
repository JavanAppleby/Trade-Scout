"""The macro sources after a restart (DESIGN 2.3 "Restart"): what the macro history says about the last read.

BLS is read weekly and Cboe once a trading day after 16:30 ET, and a restart does not read them
again while the history holds a read that still counts (`last_read`). Without a seed the board kept
them at "starting" until the next read (up to a week for BLS), and SS 10.1 makes every answer that
depends on a source not `ok` unknown. So at start `seed_board` gives each seeded source (`SEEDED`:
bls, cboe_vix, nowflation; fomc has no board entry) the status of its latest history line,
re-checked against now:

- an `ok` line stays `ok` only while it is still fresh by the source's own rules (`recheck`): its
  `stale_after` has not passed; BLS still lists a CPI and an NFP release after now; Cboe's close is
  not older than the previous trading day; nowflation's `source_generated_at` is not older than
  `nowflation_max_age_h` and, after `nowflation_fresh_after_et`, is today's. Otherwise it is `stale`
  with the first failing reason. A seed never makes a line `ok` that was not `ok` when read.
- a line read or generated more than `FUTURE_SLACK` after now (the clock went back, or a copied
  history) is never `ok`: `stale` with `read_after_now` / `generated_in_future`, as the nowflation
  reader would say for its own `generated_at`.
- a `stale` or `unavailable` line keeps its status and first reason.
- a line the seed cannot read (a hand-edited or damaged history: a field of the wrong type) is
  `stale` with `history_unreadable`; it never stops Scout from starting.
- `last_ok` is the time of the source's latest `ok` line (`MacroWriter.last_ok`); the reader's own
  `last_ok` is set to it too, so a failed read after the restart still reports it.

Polled feeds (EDGAR Atom, the wires, the halt feed) are not seeded: they poll within a minute or two
and their gap rules must start from a real poll.

Retry rule (`last_read`): the spacing (BLS weekly, Cboe once per trading-day slot, nowflation once a
day at start) counts from the last read the host answered (`ok` or `stale`). A latest line that is
`unavailable` (a network failure, an HTTP error, schema drift) did not use up the slot, so a restart
reads again once, at start; within one run a failed read is not retried before its next scheduled
time (BLS a week after the attempt, Cboe the next trading day's slot), as before. A restart is how
the owner retries after fixing the cause (Cboe's first start on Saturday 2026-10-31 failed, and the
restart found the slot used up until Monday 16:30 ET).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from scout_reader.calendar import calendar_covers, previous_trading_day

from scout.clock import et_date, et_datetime, parse_iso
from scout.config import ScoutConfig
from scout.models import SourceState
from scout.nowflation import FUTURE_SLACK

log = logging.getLogger("scout.jobs_macro")

SEEDED = ("bls", "cboe_vix", "nowflation")
ANSWERED = frozenset({"ok", "stale"})  # the host answered: the read counts for the spacing
_STATUSES = frozenset({"ok", "stale", "unavailable"})

__all__ = ["ANSWERED", "SEEDED", "last_read", "recheck", "seed_board", "seeded_state"]


def last_read(writer: Any, source: str) -> datetime | None:
    """When `source` was last read in a way that counts for its spacing (the latest history line,
    when the host answered it); None when there is none or that line failed (see the module doc)."""
    rec = (getattr(writer, "latest", None) or {}).get(source)
    if not isinstance(rec, Mapping) or rec.get("status") not in ANSWERED:
        return None
    return parse_iso(rec.get("fetched_at")) or parse_iso(rec.get("first_seen_at"))


def _vix_day(rec: Mapping[str, Any]) -> date | None:
    vix = rec.get("vix")
    if not isinstance(vix, Mapping):
        return None
    try:
        return date.fromisoformat(str(vix.get("date")))
    except ValueError:
        return None


def recheck(cfg: ScoutConfig, rec: Mapping[str, Any], now: datetime) -> list[str]:
    """Why an `ok` history line is no longer fresh at `now` (empty: still fresh)."""
    source = rec.get("source")
    ahead_of_now = now + FUTURE_SLACK
    if any((t := parse_iso(rec.get(k))) is not None and t > ahead_of_now for k in ("first_seen_at", "fetched_at")):
        return ["read_after_now"]
    gen = parse_iso(rec.get("source_generated_at"))
    if gen is not None and gen > ahead_of_now:
        return ["generated_in_future"]
    until = parse_iso(rec.get("stale_after"))
    if until is None:
        return ["no_stale_after"]
    if now >= until:
        return ["stale_after_passed"]
    today = et_date(now)
    if source == "bls":
        ahead = {str(e.get("kind")) for e in rec.get("events") or ()
                 if isinstance(e, Mapping) and (parse_iso(e.get("event_at")) or now) > now}
        return [] if {"cpi", "nfp"} <= ahead else ["no_cpi_and_nfp_after_now"]
    if source == "cboe_vix":
        day = _vix_day(rec)
        if day is None:
            return ["no_vix_close"]
        if not calendar_covers(today):
            return ["calendar_not_covered"]
        return ["vix_close_older_than_previous_trading_day"] if day < previous_trading_day(today) else []
    if source == "nowflation":
        if gen is None:
            return ["no_generated_at"]
        out = []
        if now - gen > timedelta(hours=cfg.nowflation_max_age_h):
            out.append("generated_at_old")
        if now >= et_datetime(today, cfg.nowflation_fresh_after_et) and et_date(gen) != today:
            out.append("generated_at_not_today")
        return out
    return []


def seeded_state(cfg: ScoutConfig, rec: Mapping[str, Any], last_ok: str | None, now: datetime) -> SourceState:
    """The board state for a source whose latest history line is `rec` (see the module doc)."""
    src = cfg.sources.get(str(rec.get("source")))
    enabled = bool(src.enabled) if src is not None else True
    status = str(rec.get("status"))
    reasons = [str(r) for r in rec.get("reasons") or ()]
    if status == "ok":
        stale = recheck(cfg, rec, now)
        if not stale:
            return SourceState(enabled=enabled, status="ok", last_ok=last_ok, last_error=None)
        status, reasons = "stale", stale
    elif status not in _STATUSES:
        status = "unavailable"
    return SourceState(enabled=enabled, status=status, last_ok=last_ok, last_error=reasons[0] if reasons else None)


def seed_board(board: Any, cfg: ScoutConfig, writer: Any, now: datetime,
               readers: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Seed the board (and each reader's `last_ok`) for the enabled `SEEDED` sources that have a
    history line. Returns source -> seeded status."""
    out: dict[str, str] = {}
    latest = getattr(writer, "latest", None) or {}
    last_oks = getattr(writer, "last_ok", None) or {}
    for source in SEEDED:
        src = cfg.sources.get(source)
        rec = latest.get(source)
        if src is None or not src.enabled or not isinstance(rec, Mapping):
            continue
        last_ok = last_oks.get(source)
        reader = (readers or {}).get(source)
        if reader is not None and last_ok is not None:
            reader.last_ok = last_ok
        try:
            state = seeded_state(cfg, rec, last_ok, now)
        except (TypeError, ValueError, AttributeError, KeyError):  # never ok, never a failed start
            state = SourceState(enabled=True, status="stale", last_ok=last_ok, last_error="history_unreadable")
        if board is not None:
            board.update(source, state)
        out[source] = state.status
        log.info("%s starts as %s from the read at %s%s", source, state.status, rec.get("first_seen_at"),
                 f" ({state.last_error})" if state.last_error else "")
    return out
