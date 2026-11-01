"""FOMC table (SCOUT_SPEC 4.1 #9; DESIGN 2.3)."""

from __future__ import annotations

from datetime import datetime, timezone

from conftest import make_config

from scout.fomc import fomc_events, fomc_record

UTC = timezone.utc


def test_default_table_at_the_decision_time(cfg) -> None:
    events = fomc_events(cfg)
    assert len(events) == 8 and {e.kind for e in events} == {"fomc"} and {e.source for e in events} == {"fomc"}
    by = {e.event_at.date().isoformat(): e.event_at for e in events}
    assert by["2026-12-09"] == datetime(2026, 12, 9, 19, 0, tzinfo=UTC)  # 14:00 EST
    assert by["2026-07-29"] == datetime(2026, 7, 29, 18, 0, tzinfo=UTC)  # 14:00 EDT
    assert [e.event_at for e in events] == sorted(e.event_at for e in events)


def test_config_drives_dates_and_time(scout_dir) -> None:
    cfg = make_config(scout_dir, fomc_dates=["2027-01-27", "2026-12-09"], fomc_decision_time_et="14:30")
    events = fomc_events(cfg)
    assert [e.event_at for e in events] == [datetime(2026, 12, 9, 19, 30, tzinfo=UTC),
                                            datetime(2027, 1, 27, 19, 30, tzinfo=UTC)]


def test_record_ok_while_a_date_lies_ahead(cfg) -> None:
    rec = fomc_record(cfg, datetime(2026, 11, 2, 14, tzinfo=UTC))
    assert rec.source == "fomc" and rec.status == "ok" and len(rec.events) == 8
    late = fomc_record(cfg, datetime(2026, 12, 10, tzinfo=UTC))
    assert late.status == "stale" and late.reasons == ("fomc_table_has_no_future_date",)
