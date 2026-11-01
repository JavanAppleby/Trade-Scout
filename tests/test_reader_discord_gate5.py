"""Gate 5's Discord earnings-schedule source (DESIGN 2.7, 3.6): block only, as-of, never `false`."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from scout_reader.api import event_before_next_session as gate5
from scout_reader.discord_schedule import schedule_hits, session_interval

from scout.config import ScoutConfig
from scout.discord_ingest import DiscordInbox
from scout.facts import FactWriter
from scout.health import HealthWriter
from scout.clock import ManualClock
from scout.paths import ScoutPaths
from scout.raw import RawStore

from conftest import et_at
from helpers_answers import due_record, fact, good_world, install_coverage
from helpers_discord import copy_fixture_inbox, schedule, write_capture
from helpers_fakes import write_records

AS_OF = et_at("2026-11-02", "19:45")  # Monday; next open Tuesday 3 Nov 09:30 ET
OUTSIDE = {"due_start": "2027-01-10", "due_end": "2027-04-05"}


@pytest.fixture(autouse=True)
def _coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    install_coverage(monkeypatch)


def _world(scout_dir: Path, cfg: ScoutConfig, symbols: tuple[str, ...], as_of: datetime = AS_OF, **params) -> None:
    good_world(scout_dir, cfg, as_of, params=not params)
    if params:
        from helpers_answers import write_params
        write_params(scout_dir, cfg, as_of - timedelta(days=60), **params)
    write_records(scout_dir, "due", [due_record(s, as_of - timedelta(minutes=1), **OUTSIDE) for s in symbols])


def _sched(symbol: str, day: str, session: str, seen: datetime, published: datetime | None = None) -> dict:
    return fact(symbol, published=published or seen - timedelta(minutes=5), seen=seen, kind="earnings_schedule",
                cls=None, source="discord:open-outcrier", guid=f"1|{day}|{session}", event_kind="earnings",
                event_source="discord:open-outcrier",
                extra={"earnings_date": day, "session": session, "message_id": "1", "post_kind": "earnings_schedule"})


def _ingest(paths: ScoutPaths, cfg: ScoutConfig, at: datetime) -> None:
    clock = ManualClock(at)
    DiscordInbox(cfg, paths, clock, FactWriter(paths, clock), RawStore(paths, cfg), HealthWriter(paths)).poll(at)


@pytest.mark.parametrize("day, session, value", [
    ("2026-11-03", "pre", "true"),       # before tomorrow's open: inside the window
    ("2026-11-03", "unknown", "true"),   # the whole ET day overlaps the window
    ("2026-11-03", "post", "false"),     # after tomorrow's open: outside
    ("2026-11-02", "post", "true"),      # today's POST session (16:00-20:00 ET) still overlaps 19:45
    ("2026-11-02", "pre", "false"),      # this morning's report is before as_of
    ("2026-11-04", "pre", "false"),
])
def test_a_listed_session_in_the_window_blocks(scout_dir: Path, cfg: ScoutConfig, day: str, session: str,
                                               value: str) -> None:
    _world(scout_dir, cfg, ("ACME",))
    f = _sched("ACME", day, session, AS_OF - timedelta(hours=10))
    write_records(scout_dir, "facts", [f])
    res = gate5(scout_dir, "ACME", AS_OF)
    assert res["value"] == value
    if value == "true":
        assert res["evidence"] == [f"discord:{f['fact_id']}"]
        assert res["reasons"] == ["event_in_window", "discord_earnings_schedule"]
    else:
        assert res["reasons"] == ["outside_due_window"]


def test_friday_list_for_monday_pre_blocks_over_the_weekend(scout_dir: Path, cfg: ScoutConfig) -> None:
    friday = et_at("2026-10-30", "19:45")
    _world(scout_dir, cfg, ("OMEG",), as_of=friday)
    write_records(scout_dir, "facts", [_sched("OMEG", "2026-11-02", "pre", friday - timedelta(hours=3))])
    assert gate5(scout_dir, "OMEG", friday)["value"] == "true"


def test_no_look_ahead(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg, ("ACME",))
    write_records(scout_dir, "facts", [_sched("ACME", "2026-11-03", "pre", AS_OF + timedelta(minutes=1))])
    assert gate5(scout_dir, "ACME", AS_OF)["value"] == "false"  # first seen after as_of: invisible
    late_post = _sched("ACME", "2026-11-03", "pre", AS_OF - timedelta(minutes=1), published=AS_OF + timedelta(hours=1))
    write_records(scout_dir, "facts", [late_post])
    assert gate5(scout_dir, "ACME", AS_OF)["value"] == "false"  # posted after as_of: invisible


def test_absence_changes_nothing_and_it_never_clears_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg, ("ACME",))
    base = gate5(scout_dir, "ACME", AS_OF)
    # a schedule for another symbol or another day leaves the answer as it was
    write_records(scout_dir, "facts", [_sched("BETA", "2026-11-03", "pre", AS_OF - timedelta(hours=1)),
                                       _sched("ACME", "2026-11-10", "pre", AS_OF - timedelta(hours=1))])
    assert gate5(scout_dir, "ACME", AS_OF) == base and base["value"] == "false"
    # inside its due window ACME is unknown; a Discord list with nothing for the window keeps it unknown
    write_records(scout_dir, "due", [due_record("ACME", AS_OF - timedelta(seconds=30), due_start="2026-10-20",
                                                due_end="2026-12-01")])
    res = gate5(scout_dir, "ACME", AS_OF)
    assert res["value"] == "unknown" and "in_due_window" in res["reasons"]


def test_off_or_older_params_ignore_the_source(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg, ("ACME",), discord_gate5_enabled=False)
    write_records(scout_dir, "facts", [_sched("ACME", "2026-11-03", "pre", AS_OF - timedelta(hours=1))])
    assert gate5(scout_dir, "ACME", AS_OF)["value"] == "false"


def test_params_without_the_key_change_nothing(scout_dir: Path, cfg: ScoutConfig) -> None:
    good_world(scout_dir, cfg, AS_OF, params=False)
    from helpers_answers import params_record

    old = params_record(cfg, AS_OF - timedelta(days=60))
    old.pop("discord_gate5_enabled")
    write_records(scout_dir, "params", [old])
    write_records(scout_dir, "due", [due_record("ACME", AS_OF - timedelta(minutes=1), **OUTSIDE)])
    write_records(scout_dir, "facts", [_sched("ACME", "2026-11-03", "pre", AS_OF - timedelta(hours=1))])
    assert gate5(scout_dir, "ACME", AS_OF)["value"] == "false"


def test_the_discord_status_is_not_a_gate_input(scout_dir: Path, cfg: ScoutConfig) -> None:
    from helpers_answers import BEAT_ONLY_SOURCES, GATE6_SOURCES, write_beats

    good_world(scout_dir, cfg, AS_OF)
    write_records(scout_dir, "due", [due_record("ACME", AS_OF - timedelta(minutes=1), **OUTSIDE)])
    statuses = {s: "ok" for s in (*GATE6_SOURCES, *BEAT_ONLY_SOURCES)} | {"discord_inbox": "stale"}
    write_beats(scout_dir, AS_OF - timedelta(seconds=20), AS_OF, every=10, sources=statuses)
    assert gate5(scout_dir, "ACME", AS_OF)["value"] == "false"
    from scout_reader.api import fresh_catalyst
    assert fresh_catalyst(scout_dir, "ACME", AS_OF)["value"] == "false"


def test_malformed_schedule_facts_never_raise() -> None:
    start, end = AS_OF, AS_OF + timedelta(hours=14)
    bad = [{"kind": "earnings_schedule", "ticker": "ACME", "first_seen_at": "2026-11-02T00:00:00Z",
            "published_at": None, "extra": x} for x in (None, {}, {"earnings_date": "nope", "session": "pre"},
                                                       {"earnings_date": "2026-11-03", "session": "noon"})]
    assert schedule_hits(bad + ["junk"], "ACME", AS_OF, start, end) == []  # type: ignore[list-item]


def test_session_intervals() -> None:
    pre = session_interval(date(2026, 11, 3), "pre")
    assert pre == (datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc), datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc))
    post = session_interval(date(2026, 11, 27), "post")  # the day after Thanksgiving closes at 13:00 ET
    assert post == (datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc), datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc))
    sat = session_interval(date(2026, 10, 31), "pre")  # not a trading day: the whole ET day
    assert sat == (datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc), datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc))


def test_ingested_fixture_blocks_through_the_reader(scout_dir: Path, cfg: ScoutConfig, paths: ScoutPaths) -> None:
    copy_fixture_inbox(paths)
    _ingest(paths, cfg, et_at("2026-11-02", "09:00"))
    _world(scout_dir, cfg, ("GAMA", "DELT", "ZETA"))
    assert gate5(scout_dir, "GAMA", AS_OF)["value"] == "true"  # PM ER today (levels card)
    assert gate5(scout_dir, "DELT", AS_OF)["value"] == "true"  # Tue pre (open-outcrier)
    assert gate5(scout_dir, "ZETA", AS_OF)["value"] == "false"  # a reaction post is not a schedule
    # and a capture first seen after the as-of is invisible to it
    write_capture(paths, "open-outcrier", AS_OF, [schedule("open-outcrier", AS_OF - timedelta(minutes=30),
                                                           "2026-11-03", "pre", ["ZETA"])])
    _ingest(paths, cfg, AS_OF + timedelta(minutes=5))
    assert gate5(scout_dir, "ZETA", AS_OF)["value"] == "false"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("title", [
    "$ACME Acme will report earnings after the bell next week",  # no date: was `unknown` (unparsed_date)
    "$ACME will report earnings on November 3",                  # dated: was an unlabelled `true`
])
def test_a_free_text_discord_post_classed_earnings_date_never_reaches_gate5(
        scout_dir: Path, cfg: ScoutConfig, enabled: bool, title: str) -> None:
    """DESIGN 2.7: a `discord` fact is record + brief only, whatever its class and the switch."""
    from scout.classify import wire_class

    assert wire_class(title, cfg.class_keywords) == "earnings_date"
    _world(scout_dir, cfg, ("ACME",), discord_gate5_enabled=enabled)
    base = gate5(scout_dir, "ACME", AS_OF)
    seen = AS_OF - timedelta(hours=2)
    write_records(scout_dir, "facts", [fact("ACME", published=seen - timedelta(minutes=5), seen=seen, kind="discord",
                                            cls="earnings_date", title=title, source="discord:stockmktnewz",
                                            guid="1234567890123456789")])
    assert gate5(scout_dir, "ACME", AS_OF) == base and base["value"] == "false"
