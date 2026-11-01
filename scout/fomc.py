"""FOMC decision dates from the config table (SCOUT_SPEC 4.1 #9, 7.3; DESIGN 2.3).

`fomc_dates` (set by hand from federalreserve.gov; 2027 tentative) at `fomc_decision_time_et`. The
table is the FOMC cross-check of nowflation's calendar, and the FOMC part of the fallback when
nowflation is not fresh. Its record says `stale` once the table has no decision date left in the
future, so a reader never treats an exhausted table as "no FOMC event".
"""

from __future__ import annotations

from datetime import datetime

from scout.clock import et_datetime, iso
from scout.config import ScoutConfig
from scout.macro import MacroEvent
from scout.models import MacroRecord

__all__ = ["fomc_events", "fomc_record"]


def fomc_events(cfg: ScoutConfig) -> list[MacroEvent]:
    """Every table date at the decision time (ET -> UTC once), earliest first."""
    return [MacroEvent(kind="fomc", event_at=et_datetime(day, cfg.fomc_decision_time_et), source="fomc",
                       title="FOMC decision", extra={"table": True})
            for day in sorted(set(cfg.fomc_dates))]


def fomc_record(cfg: ScoutConfig, now: datetime) -> MacroRecord:
    """The table as a history record (source "fomc"): `ok` while a date lies ahead, else `stale`."""
    events = fomc_events(cfg)
    ahead = [e for e in events if e.event_at >= now]
    status = "ok" if ahead else "stale"
    reasons = () if ahead else ("fomc_table_has_no_future_date",)
    stamp = iso(now)
    return MacroRecord(source="fomc", first_seen_at=stamp, fetched_at=stamp, status=status, reasons=reasons,
                       source_generated_at=None, sha256=None, events=tuple(e.to_dict() for e in events),
                       realtime=None, inflation_regime=None, forecast_lean=(), vix=None, attribution=None)
