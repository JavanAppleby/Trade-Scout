"""Gate 5, event_before_next_session (SCOUT_SPEC 7.2, 14; DESIGN 3.6), through the reader."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from scout_reader.api import event_before_next_session as gate5

from scout.config import ScoutConfig
from scout.due_window import due_window

from conftest import et_at
from helpers_answers import due_record, fact, good_world, install_coverage, load_submissions, write_beats, write_params
from helpers_fakes import write_records

AS_OF = et_at("2026-11-03", "19:45")  # Tuesday; next open Wednesday 4 Nov 09:30 ET
S = "ACME"
OUTSIDE = {"due_start": "2027-01-10", "due_end": "2027-04-05"}


@pytest.fixture(autouse=True)
def _coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    install_coverage(monkeypatch)


def _world(scout_dir: Path, cfg: ScoutConfig, as_of: datetime = AS_OF, **due: object) -> Path:
    good_world(scout_dir, cfg, as_of)
    if due.pop("none", False) is not True:
        write_records(scout_dir, "due", [due_record(S, as_of - timedelta(minutes=1), **{**OUTSIDE, **due})])
    return scout_dir


def _announce(scout_dir: Path, title: str, published: datetime, **kw: object) -> dict:
    f = fact(S, published=published, cls="earnings_date", title=title, **kw)
    write_records(scout_dir, "facts", [f])
    return f


def test_outside_the_due_window_with_no_announcement_is_false(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    res = gate5(scout_dir, S, AS_OF)
    assert res["value"] == "false" and res["reasons"] == ["outside_due_window"] and res["stale"] is False
    assert res["window"] == ["2026-11-04T00:45:00Z", "2026-11-04T14:30:00Z"]
    assert res["evidence"][0].startswith("due:")


@pytest.mark.parametrize("day, value", [("November 3, 2026", "true"), ("November 4, 2026", "true"),
                                        ("Nov. 4", "true"), ("November 5, 2026", "false"),
                                        ("November 2, 2026", "false")])
def test_announced_date_in_the_window(scout_dir: Path, cfg: ScoutConfig, day: str, value: str) -> None:
    _world(scout_dir, cfg)
    f = _announce(scout_dir, f"Acme to Report Third Quarter Results on {day}", AS_OF - timedelta(days=10))
    res = gate5(scout_dir, S, AS_OF)
    assert res["value"] == value
    if value == "true":
        assert res["evidence"] == [f["fact_id"]] and res["reasons"] == ["event_in_window"]


def test_announcement_beats_a_bad_due_record(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg, none=True)
    _announce(scout_dir, "Acme to Report Results on November 4, 2026", AS_OF - timedelta(days=3))
    assert gate5(scout_dir, S, AS_OF)["value"] == "true"


def test_unparseable_announcement_gives_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    f = _announce(scout_dir, "Acme to Report Third Quarter 2026 Results", AS_OF - timedelta(days=2))
    res = gate5(scout_dir, S, AS_OF)
    assert res["value"] == "unknown" and res["reasons"] == [f"unparsed_date:{f['fact_id']}"]


def test_lookback_and_as_of_limit_the_announcements(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    _announce(scout_dir, "Acme to Report Results", AS_OF - timedelta(days=46))  # older than 45 days
    _announce(scout_dir, "Acme to Report Results on November 4, 2026", AS_OF - timedelta(minutes=5),
              seen=AS_OF + timedelta(seconds=1))  # first seen after as_of
    _announce(scout_dir, "Acme to Report Results on November 4, 2026", AS_OF + timedelta(minutes=5))  # future
    assert gate5(scout_dir, S, AS_OF)["value"] == "false"


@pytest.mark.parametrize("status", ["no_periodic_200d", "no_cik", "fetch_failed", "schema", "no_http"])
def test_due_status_not_ok_gives_unknown(scout_dir: Path, cfg: ScoutConfig, status: str) -> None:
    _world(scout_dir, cfg, status=status, due_start=None, due_end=None)
    res = gate5(scout_dir, S, AS_OF)
    assert res["value"] == "unknown" and res["reasons"] == [f"due_status:{status}"]


@pytest.mark.parametrize("due, reason", [
    ({"nt_recent": True}, "nt_recent"),
    ({"overdue": True}, "overdue"),
    ({"due_start": "2026-08-10", "due_end": "2026-11-02"}, "overdue"),  # past its window as of today
    ({"due_start": "2026-11-04", "due_end": "2026-12-20"}, "in_due_window"),  # the next trading day is inside
    ({"due_start": "2026-10-01", "due_end": "2026-11-03"}, "in_due_window"),  # today is the last day
    ({"nt_recent": None}, "due_field:nt_recent"),
    ({"due_start": "bad"}, "due_field:window"),
])
def test_due_record_rules(scout_dir: Path, cfg: ScoutConfig, due: dict, reason: str) -> None:
    _world(scout_dir, cfg, **due)
    res = gate5(scout_dir, S, AS_OF)
    assert res["value"] == "unknown" and reason in res["reasons"]


def test_window_starting_after_the_next_trading_day_is_false(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg, due_start="2026-11-05", due_end="2026-12-20")
    assert gate5(scout_dir, S, AS_OF)["value"] == "false"


def test_due_record_must_be_visible_and_from_today(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg, none=True)
    assert gate5(scout_dir, S, AS_OF)["reasons"] == ["no_due_record"]
    write_records(scout_dir, "due", [due_record(S, AS_OF + timedelta(seconds=1), **OUTSIDE)])  # after as_of
    assert gate5(scout_dir, S, AS_OF)["reasons"] == ["no_due_record"]
    write_records(scout_dir, "due", [due_record(S, AS_OF - timedelta(days=1), **OUTSIDE)])  # yesterday
    assert gate5(scout_dir, S, AS_OF)["reasons"] == ["no_due_record"]
    write_records(scout_dir, "due", [due_record("OTHER", AS_OF - timedelta(minutes=5), **OUTSIDE)])
    assert gate5(scout_dir, S, AS_OF)["reasons"] == ["no_due_record"]
    write_records(scout_dir, "due", [due_record(S, AS_OF - timedelta(hours=3), **OUTSIDE)])  # same trading day
    assert gate5(scout_dir, S, AS_OF)["value"] == "false"


def test_latest_due_record_governs(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    write_records(scout_dir, "due", [due_record(S, AS_OF - timedelta(seconds=10), nt_recent=True, **OUTSIDE)])
    assert gate5(scout_dir, S, AS_OF)["reasons"] == ["nt_recent"]
    assert gate5(scout_dir, S, AS_OF - timedelta(seconds=30))["value"] == "false"


def test_friday_window_runs_to_monday_open(scout_dir: Path, cfg: ScoutConfig) -> None:
    friday = et_at("2026-11-06", "19:45")
    _world(scout_dir, cfg, friday)
    res = gate5(scout_dir, S, friday)
    assert res["value"] == "false" and res["window"][1] == "2026-11-09T14:30:00Z"
    _announce(scout_dir, "Acme to Report Results on Monday, November 9, 2026", friday - timedelta(days=4))
    assert gate5(scout_dir, S, friday)["value"] == "true"


def test_friday_due_window_opening_monday_is_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    friday = et_at("2026-11-06", "19:45")
    _world(scout_dir, cfg, friday, due_start="2026-11-09", due_end="2026-12-20")
    assert gate5(scout_dir, S, friday)["reasons"] == ["in_due_window"]


def test_pre_holiday_window_runs_past_thanksgiving(scout_dir: Path, cfg: ScoutConfig) -> None:
    eve = et_at("2026-11-25", "19:45")  # Wednesday; Thursday 26 Nov is a holiday
    _world(scout_dir, cfg, eve)
    res = gate5(scout_dir, S, eve)
    assert res["value"] == "false" and res["window"][1] == "2026-11-27T14:30:00Z"
    _announce(scout_dir, "Acme to Report Results on Nov. 27", eve - timedelta(days=4))
    assert gate5(scout_dir, S, eve)["value"] == "true"


def test_early_close_window(scout_dir: Path, cfg: ScoutConfig) -> None:
    early = et_at("2026-11-27", "16:45")  # early close day, as_of 3 hours earlier
    _world(scout_dir, cfg, early)
    assert gate5(scout_dir, S, early)["window"] == ["2026-11-27T21:45:00Z", "2026-11-30T14:30:00Z"]


def test_no_calendar_coverage_gives_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    last = et_at("2027-12-31", "19:45")  # the next open is in 2028, which the calendar does not cover
    _world(scout_dir, cfg, last)
    res = gate5(scout_dir, S, last)
    assert res["value"] == "unknown" and res["reasons"] == ["calendar_not_covered"]


def test_macro_events_count_only_when_the_owner_says(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    cpi = fact(None, published=AS_OF - timedelta(days=5), cls=None, kind="macro_event", source="nowflation",
               event_at="2026-11-04T13:30:00Z", event_kind="CPI")
    write_records(scout_dir, "facts", [cpi])
    assert gate5(scout_dir, S, AS_OF)["value"] == "false"
    write_params(scout_dir, cfg, AS_OF - timedelta(hours=1), hold_block_macro_events=True)
    res = gate5(scout_dir, S, AS_OF)
    assert res["value"] == "true" and res["evidence"] == [f"macro:{cpi['fact_id']}"]


def test_source_not_ok_and_stale(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    write_beats(scout_dir, AS_OF, AS_OF, sources={"edgar_atom": "ok", "globenewswire": "ok", "prnewswire": "stale"})
    assert gate5(scout_dir, S, AS_OF)["reasons"] == ["source_not_ok:prnewswire"]
    later = AS_OF + timedelta(minutes=10)
    res = gate5(scout_dir, S, later)
    assert res["value"] == "unknown" and res["stale"] is True


def test_end_to_end_with_the_due_window_from_a_fixture(scout_dir: Path, cfg: ScoutConfig) -> None:
    """The calendar-year fixture: inside its Q3 window on 4 Nov 2026, outside after the 10-Q."""
    info, _raw = load_submissions("submissions_calendar_year_SYNTHETIC.json")
    for day, expected in ((date(2026, 11, 3), "unknown"), (date(2026, 11, 10), "false")):
        as_of = et_at(day, "19:45")
        good_world(scout_dir, cfg, as_of)
        write_records(scout_dir, "due", [due_window(info, cfg, as_of - timedelta(minutes=1)).to_record() | {
            "ticker": S}])
        assert gate5(scout_dir, S, as_of)["value"] == expected, day


def test_labels_never_change_gate5(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    before = gate5(scout_dir, S, AS_OF)
    write_records(scout_dir, "labels", [{"labelled_at": "2026-11-03T20:00:00Z", "ticker": S, "scheduled_event": {
        "kind": "earnings", "date": "2026-11-04"}, "status": "labelled", "verified": True}])
    assert gate5(scout_dir, S, AS_OF) == before


def test_a_financial_results_for_date_notice_blocks_the_hold_end_to_end(scout_dir: Path, cfg: ScoutConfig) -> None:
    """Regression: "to Announce Financial Results for ... on November 3" was classed earnings_result,
    so gate 6 said true and gate 5 never read the date: hold_allowed was True the evening before the
    report. Classified by Scout's own wire_class, it is now earnings_date: gate 6 does not count it
    and gate 5 finds the event."""
    from scout_reader.api import fresh_catalyst
    from scout_reader.gates import hold_allowed

    from scout.classify import wire_class
    as_of = et_at("2026-11-02", "19:40")
    title = "Acme to Announce Financial Results for Third Quarter 2026 on November 3, 2026"
    _world(scout_dir, cfg, as_of)
    f = fact(S, published=et_at("2026-11-02", "17:40"), cls=wire_class(title, cfg.class_keywords), title=title)
    write_records(scout_dir, "facts", [f])
    g6, g5 = fresh_catalyst(scout_dir, S, as_of), gate5(scout_dir, S, as_of)
    assert g6["value"] == "false"
    assert g5["value"] == "true" and g5["evidence"] == [f["fact_id"]]
    assert hold_allowed(g6, g5) is False
