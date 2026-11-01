"""scout.due_window: the EDGAR due window (SCOUT_SPEC 7.2 item 2) and its replay."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from scout.clock import et_datetime, parse_hhmm
from scout.config import ScoutConfig
from scout.due_window import (
    due_window, filing_accepted, is_fiscal_year_end, next_period_end, old_rule_outside, replay,
)
from scout.models import Filing, PeriodicInfo

from helpers_answers import load_submissions

CAL = "submissions_calendar_year_SYNTHETIC.json"
JUN = "submissions_june_fy_SYNTHETIC.json"


def _at(day: str, hhmm: str = "19:45") -> datetime:
    return et_datetime(date.fromisoformat(day), parse_hhmm(hhmm))


def _q(report: str, accepted: str, form: str = "10-Q", acc: str = "x") -> Filing:
    return Filing(form=form, accepted=accepted, filing_date=accepted[:10], accession=acc, report_date=report)


@pytest.mark.parametrize("report, expected", [
    ("2026-09-30", "2026-12-31"), ("2026-03-31", "2026-06-30"), ("2026-06-30", "2026-09-30"),
    ("2025-12-31", "2026-03-31"), ("2026-11-30", "2027-02-28"), ("2027-11-30", "2028-02-29"),
    ("2026-09-27", "2026-12-27"), ("2026-12-28", "2027-03-28"),
])
def test_next_period_end(report: str, expected: str) -> None:
    assert next_period_end(date.fromisoformat(report)) == date.fromisoformat(expected)


def test_is_fiscal_year_end() -> None:
    assert is_fiscal_year_end(date(2026, 12, 31), "1231")
    assert not is_fiscal_year_end(date(2026, 9, 30), "1231")
    assert is_fiscal_year_end(date(2027, 1, 2), "1231")  # 52/53-week year across the boundary
    assert is_fiscal_year_end(date(2026, 6, 27), "0630")
    assert not is_fiscal_year_end(date(2026, 6, 20), "0630")
    with pytest.raises(ValueError):
        is_fiscal_year_end(date(2026, 6, 30), "June")


def test_quarter_and_annual_windows(cfg: ScoutConfig) -> None:
    now = _at("2026-11-20")
    q2 = PeriodicInfo(ticker="abc", cik=7, status="ok", fiscal_year_end="1231", fetched_at="f",
                      filings=(_q("2026-06-30", "2026-08-10T20:30:00Z"),))
    rec = due_window(q2, cfg, _at("2026-08-20"))
    assert (rec.status, rec.next_period_end, rec.next_is_fiscal_year_end) == ("ok", "2026-09-30", False)
    assert (rec.due_start, rec.due_end) == ("2026-10-10", "2026-11-19")
    assert rec.ticker == "ABC" and rec.submissions_fetched_at == "f" and rec.overdue is False
    q3 = PeriodicInfo(ticker="ABC", cik=7, status="ok", fiscal_year_end="1231",
                      filings=(_q("2026-09-30", "2026-11-09T21:30:00Z"),))
    rec = due_window(q3, cfg, now)
    assert (rec.next_period_end, rec.next_is_fiscal_year_end) == ("2026-12-31", True)
    assert (rec.due_start, rec.due_end) == ("2027-01-10", "2027-04-05")  # +10, +95


def test_unknown_fiscal_year_end_uses_the_longer_window(cfg: ScoutConfig) -> None:
    info = PeriodicInfo(ticker="ABC", cik=7, status="ok", fiscal_year_end=None,
                        filings=(_q("2026-06-30", "2026-08-10T20:30:00Z"),))
    rec = due_window(info, cfg, _at("2026-08-20"))
    assert rec.next_is_fiscal_year_end is None and rec.due_end == "2027-01-03"  # 2026-09-30 + 95
    bad = PeriodicInfo(ticker="ABC", cik=7, status="ok", fiscal_year_end="99xx",
                       filings=(_q("2026-06-30", "2026-08-10T20:30:00Z"),))
    assert due_window(bad, cfg, _at("2026-08-20")).due_end == "2027-01-03"


@pytest.mark.parametrize("status", ["no_cik", "fetch_failed", "schema", "no_http"])
def test_status_passes_through(cfg: ScoutConfig, status: str) -> None:
    rec = due_window(PeriodicInfo(ticker="ABC", cik=None, status=status), cfg, _at("2026-11-02"))
    assert rec.status == status and rec.due_start is None and rec.overdue is None


def test_no_periodic_in_200_days(cfg: ScoutConfig) -> None:
    old = PeriodicInfo(ticker="ABC", cik=7, status="ok", fiscal_year_end="1231",
                       filings=(_q("2025-12-31", "2026-03-10T21:00:00Z", form="10-K"),))
    assert due_window(old, cfg, _at("2026-09-20")).status == "ok"  # 194 days
    assert due_window(old, cfg, _at("2026-10-05")).status == "no_periodic_200d"  # 209 days
    none = PeriodicInfo(ticker="ABC", cik=7, status="ok", filings=(
        Filing(form="8-K", accepted="2026-10-01T12:00:00Z", filing_date="2026-10-01", accession="k", items=("2.02",)),))
    assert due_window(none, cfg, _at("2026-10-05")).status == "no_periodic_200d"
    undated = PeriodicInfo(ticker="ABC", cik=7, status="ok", filings=(_q("", "2026-08-10T20:30:00Z"),))
    assert due_window(undated, cfg, _at("2026-08-20")).status == "no_periodic_200d"


def test_overdue_and_nt(cfg: ScoutConfig) -> None:
    q2 = (_q("2026-06-30", "2026-08-10T20:30:00Z"),)
    info = PeriodicInfo(ticker="ABC", cik=7, status="ok", fiscal_year_end="1231", filings=q2)
    assert due_window(info, cfg, _at("2026-11-19")).overdue is False
    assert due_window(info, cfg, _at("2026-11-20")).overdue is True
    nt = Filing(form="NT 10-Q", accepted="2026-11-16T21:00:00Z", filing_date="2026-11-16", accession="nt1")
    info = PeriodicInfo(ticker="ABC", cik=7, status="ok", fiscal_year_end="1231", filings=q2 + (nt,))
    rec = due_window(info, cfg, _at("2026-11-18"))
    assert rec.nt_recent is True and rec.nt_accession == "nt1"
    assert due_window(info, cfg, _at("2026-11-15")).nt_recent is False  # not accepted yet
    assert due_window(info, cfg, _at("2027-01-05")).nt_recent is False  # older than 45 days


def test_filings_after_now_are_never_used(cfg: ScoutConfig) -> None:
    info = PeriodicInfo(ticker="ABC", cik=7, status="ok", fiscal_year_end="1231", filings=(
        _q("2026-06-30", "2026-08-10T20:30:00Z", acc="q2"), _q("2026-09-30", "2026-11-09T21:30:00Z", acc="q3")))
    before = due_window(info, cfg, _at("2026-11-09", "16:00"))
    after = due_window(info, cfg, _at("2026-11-09", "17:00"))
    assert before.last_periodic_accession == "q2" and before.report_date == "2026-06-30"
    assert after.last_periodic_accession == "q3" and after.report_date == "2026-09-30"


def test_amendment_of_an_old_period_does_not_move_the_window(cfg: ScoutConfig) -> None:
    info = PeriodicInfo(ticker="ABC", cik=7, status="ok", fiscal_year_end="1231", filings=(
        _q("2026-06-30", "2026-08-10T20:30:00Z", acc="q2"),
        _q("2026-03-31", "2026-09-01T20:30:00Z", form="10-Q/A", acc="q1a")))
    assert due_window(info, cfg, _at("2026-09-02")).last_periodic_accession == "q2"


def test_filing_accepted_falls_back_to_the_filing_date() -> None:
    f = Filing(form="10-Q", accepted="garbage", filing_date="2026-11-09", accession="a")
    assert filing_accepted(f) == et_datetime(date(2026, 11, 9), parse_hhmm("00:00"))
    assert filing_accepted(Filing(form="10-Q", accepted="", filing_date="", accession="a")) is None


# --------------------------------------------------------------------------- fixtures per fiscal quarter


@pytest.mark.parametrize("name, inside, outside", [
    # calendar fiscal year: Q1, Q2, Q3 windows, then the 10-K season window
    (CAL, ["2025-04-10", "2025-05-06", "2025-05-09"], ["2025-04-08", "2025-05-13", "2025-06-20"]),
    (CAL, ["2025-07-10", "2025-08-05"], ["2025-07-08", "2025-08-12", "2025-09-15"]),
    (CAL, ["2025-10-10", "2025-11-04"], ["2025-10-08", "2025-11-11", "2025-12-15"]),
    (CAL, ["2026-01-09", "2026-02-20", "2026-03-03", "2026-03-13"], ["2026-01-07", "2026-03-17"]),
    # June fiscal year: Q1 (Sep), Q2 (Dec), Q3 (Mar), 10-K season (Jun year end)
    (JUN, ["2025-10-10", "2025-11-04"], ["2025-10-08", "2025-11-10"]),
    (JUN, ["2026-01-09", "2026-02-03"], ["2026-01-07", "2026-02-09"]),
    (JUN, ["2026-04-10", "2026-05-05"], ["2026-04-08", "2026-05-11"]),
    (JUN, ["2025-07-10", "2025-08-19", "2025-09-11"], ["2025-07-08", "2025-09-15"]),
])
def test_due_window_per_fiscal_quarter(cfg: ScoutConfig, name: str, inside: list[str], outside: list[str]) -> None:
    info, raw = load_submissions(name)
    assert raw["_SYNTHETIC"]
    rows = {r.day.isoformat(): r for r in replay(info.filings, info.fiscal_year_end, cfg, date(2025, 1, 1),
                                                   date(2026, 12, 31))}
    for day in inside:
        assert not rows[day].outside, f"{name} {day} should be inside its due window (unknown)"
    for day in outside:
        assert rows[day].outside, f"{name} {day} should be outside its due window (false)"


@pytest.mark.parametrize("name", [CAL, JUN])
def test_replay_has_no_misses(cfg: ScoutConfig, name: str) -> None:
    info, _raw = load_submissions(name)
    rows = replay(info.filings, info.fiscal_year_end, cfg, date(2025, 1, 2), date(2026, 11, 30))
    reported = [r for r in rows if r.reported_before_open]
    assert len(reported) >= 7  # every pre-market earnings 8-K lands in some row's window
    assert [r.day for r in rows if r.miss] == []
    assert sum(r.outside for r in rows) > 100  # the rule does answer false most days


@pytest.mark.parametrize("name, eve", [(CAL, date(2026, 3, 3)), (JUN, date(2025, 8, 26))])
def test_old_rule_misses_in_10k_season(cfg: ScoutConfig, name: str, eve: date) -> None:
    """SS 7.2: 'last filing + 75-105 days' falls outside its own window in 10-K season."""
    info, _raw = load_submissions(name)
    row = next(r for r in replay(info.filings, info.fiscal_year_end, cfg, eve, eve))
    assert row.reported_before_open  # the earnings 8-K comes before the next open
    assert old_rule_outside(info.filings, eve)  # the old rule would have said false: a miss
    assert not row.outside  # the period-end rule says unknown


def test_replay_skips_non_trading_days(cfg: ScoutConfig) -> None:
    info, _raw = load_submissions(CAL)
    rows = replay(info.filings, info.fiscal_year_end, cfg, date(2026, 11, 26), date(2026, 11, 30))
    assert [r.day for r in rows] == [date(2026, 11, 27), date(2026, 11, 30)]  # Thanksgiving and the weekend skipped
    assert rows[0].as_of == datetime(2026, 11, 28, 0, 45, tzinfo=timezone.utc)
    assert old_rule_outside((), date(2026, 11, 27)) is False
    assert rows[1].as_of - rows[0].as_of == timedelta(days=3)
