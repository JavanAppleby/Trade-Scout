"""The EDGAR due window, gate 5 source 2 (SCOUT_SPEC 7.2 item 2, DESIGN 2.5 and 5.6). Pure.

Keyed on the report period, not the filing date:
- Take the last periodic report (10-Q or 10-K, amendments included) by `reportDate` (the period
  end) and the company's `fiscalYearEnd` ("MMDD").
- The next period end P is `reportDate` + 3 calendar months (a month-end date stays a month end:
  2026-09-30 -> 2026-12-31).
- If P is a fiscal year end (within 7 days of `fiscalYearEnd`, across a year boundary too), the
  next report is due in [P + `earnings_due_start_days`, P + `earnings_due_end_days_annual`];
  otherwise in [P + `earnings_due_start_days`, P + `earnings_due_end_days_quarter`]. An unknown
  fiscal year end uses the longer annual window (more `unknown`, never less).
- `overdue` once today (ET) is past `due_end`; `nt_recent` when an NT 10-Q / NT 10-K was accepted
  in the last `nt_recent_days`; status `no_periodic_200d` when no periodic report was accepted in
  the last `periodic_max_age_days`.
Only filings accepted at or before `now` count, so a replay never sees the future.

`replay(...)` walks trading days and reports where the rule would have answered `false` while a
periodic report or an 8-K item 2.02 (earnings) was accepted before the next open: a miss.
"""

from __future__ import annotations

import calendar as _monthcal
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from scout_reader import calendar as cal
from scout_reader.records import parse_time

from scout.clock import et_date, et_datetime, iso
from scout.models import DueWindowRecord, Filing, PeriodicInfo

__all__ = [
    "NT_FORMS", "PERIODIC_FORMS", "ReplayRow", "due_window", "filing_accepted", "is_fiscal_year_end",
    "next_period_end", "old_rule_outside", "replay",
]

PERIODIC_FORMS = frozenset({"10-Q", "10-K", "10-KT", "10-Q/A", "10-K/A", "10-KT/A"})
NT_FORMS = frozenset({"NT 10-Q", "NT 10-K", "NT 10-Q/A", "NT 10-K/A"})
FYE_TOLERANCE_DAYS = 7
REPLAY_AS_OF_ET = time(19, 45)


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    last = _monthcal.monthrange(year, month)[1]
    if day.day == _monthcal.monthrange(day.year, day.month)[1]:
        return date(year, month, last)  # a month end stays a month end
    return date(year, month, min(day.day, last))


def next_period_end(report_date: date) -> date:
    """`report_date` + 3 calendar months, end-of-month kept and clamped."""
    return _add_months(report_date, 3)


def _fye_dates(fiscal_year_end_mmdd: str, around: date) -> list[date]:
    text = str(fiscal_year_end_mmdd).strip()
    if len(text) != 4 or not text.isdigit():
        raise ValueError(f"fiscalYearEnd must be MMDD, got {fiscal_year_end_mmdd!r}")
    month, day = int(text[:2]), int(text[2:])
    out: list[date] = []
    for year in (around.year - 1, around.year, around.year + 1):
        last = _monthcal.monthrange(year, month)[1] if 1 <= month <= 12 else 0
        if not 1 <= day <= 31 or last == 0:
            raise ValueError(f"fiscalYearEnd is not a date: {fiscal_year_end_mmdd!r}")
        out.append(date(year, month, min(day, last)))
    return out


def is_fiscal_year_end(period_end: date, fiscal_year_end_mmdd: str) -> bool:
    """True when `period_end` is within 7 days of the fiscal year end (52/53-week years included)."""
    return any(abs((period_end - d).days) <= FYE_TOLERANCE_DAYS for d in _fye_dates(fiscal_year_end_mmdd, period_end))


def filing_accepted(f: Filing) -> datetime | None:
    """When the filing was accepted (aware UTC); a bare filing date counts from 00:00 ET."""
    at = parse_time(f.accepted)
    if at is not None:
        return at
    for text in (f.accepted, f.filing_date):
        try:
            day = date.fromisoformat(str(text)[:10])
        except ValueError:
            continue
        return et_datetime(day, time(0, 0))
    return None


def _form(f: Filing) -> str:
    return str(f.form).strip().upper()


def _report_date(f: Filing) -> date | None:
    try:
        return date.fromisoformat(str(f.report_date)) if f.report_date else None
    except ValueError:
        return None


def _empty(info: PeriodicInfo, now: datetime, status: str) -> DueWindowRecord:
    return DueWindowRecord(
        ticker=info.ticker.upper(), cik=info.cik, computed_at=iso(now), status=status, last_periodic_form=None,
        last_periodic_accession=None, report_date=None, fiscal_year_end=info.fiscal_year_end, next_period_end=None,
        next_is_fiscal_year_end=None, due_start=None, due_end=None, overdue=None, nt_recent=None, nt_accession=None,
        submissions_fetched_at=info.fetched_at)


def due_window(info: PeriodicInfo, cfg: Any, now: datetime) -> DueWindowRecord:
    """The due-window record for one company at `now` (aware). Never raises for bad filings: a row
    without a readable date or report period is skipped."""
    if info.status != "ok":
        return _empty(info, now, info.status if info.status else "fetch_failed")
    seen: list[tuple[datetime, Filing]] = []
    for f in info.filings:
        at = filing_accepted(f)
        if at is not None and at <= now:
            seen.append((at, f))
    periodic = [(at, f) for at, f in seen if _form(f) in PERIODIC_FORMS]
    max_age = timedelta(days=int(cfg.periodic_max_age_days))
    if not any(now - at <= max_age for at, _f in periodic):
        return _empty(info, now, "no_periodic_200d")
    dated = [(rd, at, f) for at, f in periodic if (rd := _report_date(f)) is not None]
    if not dated:
        return _empty(info, now, "no_periodic_200d")
    report, _at, last = max(dated, key=lambda row: (row[0], row[1]))
    period_end = next_period_end(report)
    annual: bool | None
    try:
        annual = is_fiscal_year_end(period_end, info.fiscal_year_end) if info.fiscal_year_end else None
    except ValueError:
        annual = None
    end_days = cfg.earnings_due_end_days_quarter if annual is False else cfg.earnings_due_end_days_annual
    due_start = period_end + timedelta(days=int(cfg.earnings_due_start_days))
    due_end = period_end + timedelta(days=int(end_days))
    nt_from = now - timedelta(days=int(cfg.nt_recent_days))
    nts = sorted(((at, f) for at, f in seen if _form(f) in NT_FORMS and at >= nt_from), key=lambda row: row[0])
    return DueWindowRecord(
        ticker=info.ticker.upper(), cik=info.cik, computed_at=iso(now), status="ok",
        last_periodic_form=_form(last), last_periodic_accession=last.accession or None,
        report_date=report.isoformat(), fiscal_year_end=info.fiscal_year_end,
        next_period_end=period_end.isoformat(), next_is_fiscal_year_end=annual, due_start=due_start.isoformat(),
        due_end=due_end.isoformat(), overdue=et_date(now) > due_end, nt_recent=bool(nts),
        nt_accession=(nts[-1][1].accession or None) if nts else None, submissions_fetched_at=info.fetched_at)


# --------------------------------------------------------------------------- replay


@dataclass(frozen=True)
class ReplayRow:
    """One trading day of a replay. `outside` is the rule's `false` (S clear of its due window);
    `miss` is a `false` while an earnings filing was accepted before the next open."""

    day: date
    as_of: datetime
    status: str
    due_start: str | None
    due_end: str | None
    outside: bool
    reported_before_open: tuple[str, ...]
    miss: bool


def _earnings_filing(f: Filing) -> bool:
    form = _form(f)
    return form in PERIODIC_FORMS or (form.startswith("8-K") and "2.02" in tuple(f.items or ()))


def _outside(rec: DueWindowRecord, day: date) -> bool:
    """The reader's rule (DESIGN 3.6) for a fresh record: false only when status ok, no NT, not
    overdue and neither `day` nor the next trading day lies in [due_start, due_end]."""
    if rec.status != "ok" or rec.nt_recent or rec.overdue or not rec.due_start or not rec.due_end:
        return False
    start, end = date.fromisoformat(rec.due_start), date.fromisoformat(rec.due_end)
    last = cal.next_trading_day(day)
    if day > end:
        return False
    return not last >= start


def replay(filings: Sequence[Filing], fiscal_year_end: str | None, cfg: Any, start: date, end: date, *,
           ticker: str = "REPLAY", cik: int | None = None, as_of_et: time = REPLAY_AS_OF_ET) -> list[ReplayRow]:
    """For each trading day in [start, end], the due record as of `as_of_et` that day and whether a
    periodic report or an 8-K 2.02 was accepted after as_of and before the next open."""
    rows: list[ReplayRow] = []
    day = start
    while day <= end:
        if not cal.is_trading_day(day):
            day += timedelta(days=1)
            continue
        as_of = et_datetime(day, as_of_et)
        info = PeriodicInfo(ticker=ticker, cik=cik, status="ok", filings=tuple(filings),
                            fiscal_year_end=fiscal_year_end, fetched_at=iso(as_of))
        rec = due_window(info, cfg, as_of)
        # Next open by trading-day arithmetic, so a replay of years the holiday table does not
        # cover still runs (weekdays count as trading days there).
        opening = cal.core_open(cal.next_trading_day(day))
        reported: list[str] = []
        for f in filings:
            at = filing_accepted(f)
            if at is not None and opening is not None and as_of < at < opening and _earnings_filing(f):
                reported.append(f"{_form(f)} {f.accession}")
        outside = _outside(rec, day)
        rows.append(ReplayRow(day=day, as_of=as_of, status=rec.status, due_start=rec.due_start, due_end=rec.due_end,
                              outside=outside, reported_before_open=tuple(reported), miss=outside and bool(reported)))
        day += timedelta(days=1)
    return rows


def old_rule_outside(filings: Iterable[Filing], day: date, low_days: int = 75, high_days: int = 105) -> bool:
    """The rule SS 7.2 replaces: 'last periodic filing + 75-105 days'. True when `day` is outside
    that window (the old rule's `false`). Used by tests to show why it was replaced."""
    last: date | None = None
    for f in filings:
        at = filing_accepted(f)
        if at is None or _form(f) not in PERIODIC_FORMS or et_date(at) > day:
            continue
        if last is None or et_date(at) > last:
            last = et_date(at)
    if last is None:
        return False
    return not (last + timedelta(days=low_days) <= day <= last + timedelta(days=high_days))
