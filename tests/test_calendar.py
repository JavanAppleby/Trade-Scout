"""Scout's calendar copy agrees with Trade Guardian's clock.py on every day of 2026-2027 (SCOUT_SPEC 3,
14 "Calendar"). The expected answers were produced by Guardian's own code with
tools/make_calendar_fixture.py and saved in tests/fixtures/guardian_calendar_2026_2027.json, so
this test never needs Trade Guardian installed."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from scout import calendar as cal
from scout.calendar import Session
from scout.clock import iso

FIXTURE = Path(__file__).parent / "fixtures" / "guardian_calendar_2026_2027.json"


@pytest.fixture(scope="module")
def guardian() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_provenance_matches_the_fixture(guardian: dict) -> None:
    assert guardian["guardian_commit"] == cal.GUARDIAN_COMMIT
    assert guardian["guardian_clock_sha256"] == cal.GUARDIAN_CLOCK_SHA256
    assert sorted(cal.CALENDAR_YEARS) == guardian["calendar_years"]
    assert len(guardian["days"]) == 730


def test_every_day_of_2026_2027_agrees_with_guardian(guardian: dict) -> None:
    mismatches: list[str] = []
    for row in guardian["days"]:
        day = date.fromisoformat(row["day"])
        mine = {
            "calendar_covers": cal.calendar_covers(day), "is_holiday": cal.is_holiday(day),
            "is_early_close": cal.is_early_close(day), "is_trading_day": cal.is_trading_day(day),
            "next_trading_day": cal.next_trading_day(day).isoformat(),
            "previous_trading_day": cal.previous_trading_day(day).isoformat(),
        }
        st = cal.session_times(day)
        mine["session_times"] = None if st is None else {
            "pre_open": iso(st.pre_open), "core_open": iso(st.core_open), "core_close": iso(st.core_close),
            "post_close": iso(st.post_close), "early_close": st.early_close}
        for key, value in mine.items():
            if row[key] != value:
                mismatches.append(f"{day} {key}: guardian {row[key]!r} scout {value!r}")
        for probe in row["probes"]:
            hh, mm = (int(x) for x in probe["et"].split(":"))
            instant = cal.et_datetime(day, time(hh, mm))
            if iso(instant) != probe["utc"]:
                mismatches.append(f"{day} {probe['et']} ET: guardian {probe['utc']} scout {iso(instant)}")
            at = _utc(probe["utc"])
            if cal.session_at(at).value != probe["session"]:
                mismatches.append(f"{probe['utc']} session: guardian {probe['session']} scout {cal.session_at(at)}")
            if cal.trading_day_of(at).isoformat() != probe["trading_day"]:
                mismatches.append(f"{probe['utc']} trading day: guardian {probe['trading_day']}")
    assert mismatches == [], mismatches[:20]


def test_session_enum_carries_guardians_names() -> None:
    assert [s.value for s in Session] == ["PRE", "CORE", "POST", "NIGHT", "CLOSED"]


def test_prior_close_and_next_open(et) -> None:  # noqa: ANN001
    # Monday 2 Nov 2026 19:45 ET: prior close is Monday 16:00 ET; next open Tuesday 09:30 ET
    as_of = et("2026-11-02", "19:45")
    assert cal.prior_close_before(as_of) == et("2026-11-02", "16:00")
    assert cal.next_open_after(as_of) == et("2026-11-03", "09:30")
    # Monday 09:00 ET (before the open): prior close is Friday 30 Oct 16:00 ET; next open today 09:30
    assert cal.prior_close_before(et("2026-11-02", "09:00")) == et("2026-10-30", "16:00")
    assert cal.next_open_after(et("2026-11-02", "09:00")) == et("2026-11-02", "09:30")
    # exactly at the close counts as that close; exactly at the open is not "after"
    assert cal.prior_close_before(et("2026-11-02", "16:00")) == et("2026-11-02", "16:00")
    assert cal.next_open_after(et("2026-11-02", "09:30")) == et("2026-11-03", "09:30")
    # Friday evening -> Monday open; pre-holiday Wednesday 25 Nov -> Friday 27 Nov (early close day)
    assert cal.next_open_after(et("2026-10-30", "19:45")) == et("2026-11-02", "09:30")
    assert cal.next_open_after(et("2026-11-25", "19:45")) == et("2026-11-27", "09:30")
    # the early-close day's close is 13:00 ET
    assert cal.prior_close_before(et("2026-11-27", "19:45")) == et("2026-11-27", "13:00")


def test_next_open_is_none_beyond_the_calendar(et) -> None:  # noqa: ANN001
    assert cal.next_open_after(et("2027-12-31", "19:45")) is None
    assert cal.next_open_after(et("2027-12-31", "08:00")) == et("2027-12-31", "09:30")


def test_in_active_hours(cfg, et) -> None:  # noqa: ANN001
    assert cal.in_active_hours(et("2026-11-02", "04:00"), cfg)
    assert cal.in_active_hours(et("2026-11-02", "19:59"), cfg)
    assert not cal.in_active_hours(et("2026-11-02", "20:00"), cfg)
    assert not cal.in_active_hours(et("2026-11-02", "03:59"), cfg)


def test_naive_times_are_refused() -> None:
    with pytest.raises(ValueError):
        cal.session_at(datetime(2026, 11, 2, 14, 0))
