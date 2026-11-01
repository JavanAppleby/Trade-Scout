"""scout_reader.halts: halt_open (SCOUT_SPEC 7.5, 2 rule 9, 14; DESIGN 3.8). A stale feed gives
unknown, never "not halted"; no look-ahead; never raises."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from helpers_fakes import FakeBoard, FakeFactSink, FakeFetcher, FakeHealthSink, FakeRawSink, write_records
from helpers_macro import HALTS_URL, halts_body

from scout.clock import iso
from scout.halts import HaltPoller
from scout_reader.halts import halt_open_at, halt_open_from, load_halt_facts


def _facts(cfg, clock, at: datetime, body: bytes | None = None) -> list[dict]:
    sink = FakeFactSink()
    poller = HaltPoller(cfg, FakeFetcher({HALTS_URL: body or halts_body()}), FakeRawSink(), sink, FakeHealthSink(),
                        FakeBoard(), clock)
    poller.poll(at)
    return [f.to_record() for f in sink.facts]


def _beat(at: datetime, status: str = "ok", gap: bool = False) -> dict:
    return {"format": 1, "at": iso(at), "stopped": False, "sources": {"nasdaq_halts": status},
            "gap_open": {"nasdaq_halts": gap}}


@pytest.fixture
def seen(cfg, clock, et) -> list[dict]:
    return _facts(cfg, clock, et("2026-11-02", "10:00"))


def test_open_halt_today_is_true(seen, et) -> None:
    at = et("2026-11-02", "10:05")
    res = halt_open_from(seen, _beat(at), "WXYZ", at)
    assert res["value"] == "true" and res["evidence"] and res["stale"] is False


def test_resumed_halt_is_false(seen, et) -> None:
    at = et("2026-11-02", "10:05")
    assert halt_open_from(seen, _beat(at), "ABCD", at)["value"] == "false"
    assert halt_open_from(seen, _beat(at), "abcd", at)["value"] == "false"  # symbols compare upper-case


def test_scheduled_resumption_counts_only_once_passed(seen, et) -> None:
    before = et("2026-11-02", "10:29")
    after = et("2026-11-02", "10:31")
    assert halt_open_from(seen, _beat(before), "NEWS", before)["value"] == "true"
    assert halt_open_from(seen, _beat(after), "NEWS", after)["value"] == "false"


def test_unresolved_halt_from_an_earlier_day_is_unknown(seen, et) -> None:
    at = et("2026-11-02", "10:05")
    res = halt_open_from(seen, _beat(at), "OLDC", at)
    assert res["value"] == "unknown" and res["reasons"] == ["halt_unresolved_since:2019-04-16"]


def test_no_halt_with_feed_ok_is_false(seen, et) -> None:
    at = et("2026-11-02", "10:05")
    assert halt_open_from(seen, _beat(at), "ZZZZ", at)["value"] == "false"


@pytest.mark.parametrize("status", ["stale", "unavailable", "disabled", "refused", "starting", None])
def test_feed_not_ok_is_unknown_never_not_halted(seen, et, status) -> None:
    at = et("2026-11-02", "10:05")
    for sym in ("WXYZ", "ABCD", "ZZZZ"):
        res = halt_open_from(seen, _beat(at, status=status), sym, at)
        assert res["value"] == "unknown" and res["reasons"] == ["source_not_ok:nasdaq_halts"]


def test_gap_open_or_stale_scout_is_unknown(seen, et) -> None:
    at = et("2026-11-02", "10:05")
    assert halt_open_from(seen, _beat(at, gap=True), "ZZZZ", at)["reasons"] == ["gap_open:nasdaq_halts"]
    res = halt_open_from(seen, None, "WXYZ", at)
    assert res["value"] == "unknown" and res["stale"] is True and res["reasons"] == ["scout_stale"]
    assert halt_open_from(seen, {"format": 1, "stopped": False}, "ZZZZ", at)["value"] == "unknown"


def test_no_look_ahead(seen, et) -> None:
    # The facts were first seen at 10:00 ET; at 09:50 ET they do not exist yet.
    at = et("2026-11-02", "09:50")
    assert halt_open_from(seen, _beat(at), "WXYZ", at)["value"] == "false"


def test_halt_after_as_of_does_not_count(cfg, clock, et) -> None:
    facts = _facts(cfg, clock, et("2026-11-02", "10:00"))
    # first_seen_at moved back so only halt_time > as_of keeps it out
    for f in facts:
        f["first_seen_at"] = "2026-11-02T12:00:00Z"
        f["published_at"] = None
    at = et("2026-11-02", "09:40")  # WXYZ halted 09:45
    assert halt_open_from(facts, _beat(at), "WXYZ", at)["value"] == "false"


def test_bad_inputs_never_raise(seen, et) -> None:
    at = et("2026-11-02", "10:05")
    assert halt_open_from(seen, _beat(at), "WXYZ", datetime(2026, 11, 2, 15))["reasons"] == ["naive_as_of"]
    assert halt_open_from(seen, _beat(at), "", at)["reasons"] == ["no_symbol"]

    class Boom:
        def __iter__(self):
            raise RuntimeError("disk on fire")

    assert halt_open_from(Boom(), _beat(at), "WXYZ", at)["reasons"] == ["error:RuntimeError"]
    junk = [None, 3, "x", {"kind": "halt", "ticker": "WXYZ"}, {**seen[0], "format": 99}]
    assert halt_open_from(junk, _beat(at), "WXYZ", at)["value"] == "false"


def test_reads_scout_files(scout_dir, cfg, clock, et) -> None:
    facts = _facts(cfg, clock, et("2026-11-02", "10:00"))
    write_records(scout_dir, "facts", facts)
    at = et("2026-11-02", "10:05")
    write_records(scout_dir, "beats", [_beat(at - timedelta(seconds=20))])
    assert len(load_halt_facts(scout_dir, at)) == 5
    assert halt_open_at(scout_dir, "WXYZ", at)["value"] == "true"
    assert halt_open_at(scout_dir, "ABCD", at)["value"] == "false"
    # five minutes later the last beat is older than the 120 s default: stale
    late = at + timedelta(minutes=5)
    res = halt_open_at(scout_dir, "WXYZ", late)
    assert res["value"] == "unknown" and res["stale"] is True
    assert halt_open_at(scout_dir, "WXYZ", datetime(2026, 11, 2))["reasons"] == ["naive_as_of"]


def test_stopped_beat_is_stale(scout_dir, cfg, clock, et) -> None:
    write_records(scout_dir, "facts", _facts(cfg, clock, et("2026-11-02", "10:00")))
    at = et("2026-11-02", "10:05")
    write_records(scout_dir, "beats", [{**_beat(at - timedelta(seconds=10)), "stopped": True}])
    assert halt_open_at(scout_dir, "ZZZZ", at)["value"] == "unknown"


def test_partial_and_corrupt_lines_are_ignored(scout_dir, cfg, clock, et) -> None:
    facts = _facts(cfg, clock, et("2026-11-02", "10:00"))
    write_records(scout_dir, "facts", facts, raw_lines=["{not json\n", '{"kind": "halt", "ticker": "QQQQ"'])
    at = et("2026-11-02", "10:05")
    write_records(scout_dir, "beats", [_beat(at)])
    assert halt_open_at(scout_dir, "WXYZ", at)["value"] == "true"
    assert halt_open_at(scout_dir, "QQQQ", at)["value"] == "false"
