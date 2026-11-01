"""Gate 6, fresh_catalyst (SCOUT_SPEC 7.1, 14; DESIGN 3.5), and news_headline, through the reader."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from scout_reader.api import Reader, fresh_catalyst, news_headline

from scout.config import ScoutConfig

from conftest import et_at
from helpers_answers import (
    GATE6_SOURCES, fact, good_world, hide_module, install_coverage, write_beats, write_health, write_params,
)
from helpers_fakes import write_records

AS_OF = et_at("2026-11-03", "19:45")  # Tuesday, EST
CLOSE = et_at("2026-11-02", "16:00")  # gate 6 window start: the prior trading day's close (SS 7.1)
S = "ACME"


@pytest.fixture(autouse=True)
def _coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    install_coverage(monkeypatch)


@pytest.fixture
def world(scout_dir: Path, cfg: ScoutConfig) -> Path:
    good_world(scout_dir, cfg, AS_OF)
    return scout_dir


def _facts(scout_dir: Path, *facts: dict) -> None:
    write_records(scout_dir, "facts", facts)


def test_no_fact_with_full_coverage_is_false(world: Path) -> None:
    res = fresh_catalyst(world, S, AS_OF)
    assert res["value"] == "false" and res["stale"] is False and res["reasons"] == ["no_fact_full_coverage"]
    assert res["window"] == ["2026-11-02T21:00:00Z", "2026-11-04T00:45:00Z"]


def test_fact_just_inside_the_window_is_true(world: Path) -> None:
    f = fact(S, published=CLOSE, seen=CLOSE + timedelta(seconds=1), cls="fda")
    _facts(world, f)
    res = fresh_catalyst(world, S, AS_OF)
    assert res["value"] == "true" and res["evidence"] == [f["fact_id"]]
    assert fresh_catalyst(world, S.lower(), AS_OF)["value"] == "true"  # symbols are upper-cased


def test_fact_just_outside_the_window_is_false(world: Path) -> None:
    _facts(world, fact(S, published=CLOSE - timedelta(seconds=1), cls="fda"))
    assert fresh_catalyst(world, S, AS_OF)["value"] == "false"


def test_published_and_first_seen_must_both_be_in_the_window(world: Path) -> None:
    _facts(world, fact(S, published=CLOSE - timedelta(minutes=5), seen=CLOSE + timedelta(minutes=5), cls="fda"))
    assert fresh_catalyst(world, S, AS_OF)["value"] == "false"


def test_fact_first_seen_after_as_of_is_invisible(world: Path) -> None:
    _facts(world, fact(S, published=AS_OF - timedelta(minutes=1), seen=AS_OF + timedelta(seconds=1), cls="fda"))
    assert fresh_catalyst(world, S, AS_OF)["value"] == "false"
    later = AS_OF + timedelta(seconds=1)
    write_beats(world, later, later)
    write_health(world, later, later)
    assert fresh_catalyst(world, S, later)["value"] == "true"


def test_other_symbols_and_kinds_do_not_count(world: Path) -> None:
    _facts(world, fact("OTHER", published=AS_OF - timedelta(minutes=5), cls="fda"),
           fact(S, published=AS_OF - timedelta(minutes=5), cls=None, kind="halt", source="nasdaq_halts"))
    assert fresh_catalyst(world, S, AS_OF)["value"] == "false"


@pytest.mark.parametrize("cls", ["fda", "trial", "contract", "contract_or_other", "earnings_result"])
def test_hold_catalyst_classes_pass(world: Path, cls: str) -> None:
    _facts(world, fact(S, published=AS_OF - timedelta(minutes=5), cls=cls, kind="filing" if "or" in cls else "wire"))
    assert fresh_catalyst(world, S, AS_OF)["value"] == "true"


@pytest.mark.parametrize("cls", ["offering", "reverse_split", "listing_notice", "other", "earnings_date"])
def test_never_pass_classes(scout_dir: Path, cfg: ScoutConfig, cls: str) -> None:
    """SS 14: these never make gate 6 true, even when the config lists them (earnings_date: third
    review, a notice that only names a date is never a catalyst)."""
    good_world(scout_dir, cfg, AS_OF, params=False)
    write_params(scout_dir, cfg, AS_OF - timedelta(days=1),
                 hold_catalyst_classes=["fda", "offering", "reverse_split", "listing_notice", "other",
                                        "earnings_date"])
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(minutes=5), cls=cls))
    assert fresh_catalyst(scout_dir, S, AS_OF)["value"] == "false"
    head = news_headline(scout_dir, S, AS_OF)
    assert head["value"] == "true" and head["classes"] == [cls]


def test_classes_follow_the_params_in_force(scout_dir: Path, cfg: ScoutConfig) -> None:
    good_world(scout_dir, cfg, AS_OF, params=False)
    write_params(scout_dir, cfg, AS_OF - timedelta(days=1), hold_catalyst_classes=["fda"])
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(minutes=5), cls="contract"))
    assert fresh_catalyst(scout_dir, S, AS_OF)["value"] == "false"
    write_params(scout_dir, cfg, AS_OF - timedelta(minutes=1), hold_catalyst_classes=["contract"])
    assert fresh_catalyst(scout_dir, S, AS_OF)["value"] == "true"
    assert fresh_catalyst(scout_dir, S, AS_OF - timedelta(seconds=61))["value"] == "false"  # older params


def test_a_coverage_gap_turns_false_into_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    write_params(scout_dir, cfg, AS_OF - timedelta(days=5))
    write_beats(scout_dir, AS_OF - timedelta(seconds=60), AS_OF)
    write_health(scout_dir, CLOSE - timedelta(minutes=10), CLOSE + timedelta(hours=1))
    write_health(scout_dir, CLOSE + timedelta(hours=2), AS_OF)  # an hour with no poll
    res = fresh_catalyst(scout_dir, S, AS_OF)
    assert res["value"] == "unknown"
    assert {f"gap:{s}:interval" for s in GATE6_SOURCES} <= set(res["reasons"])


@pytest.mark.parametrize("result, overlap, why", [("ok", False, "no_overlap"), ("timeout", True, "failed_poll"),
                                                  ("http_error", None, "failed_poll")])
def test_a_bad_poll_turns_false_into_unknown(world: Path, result: str, overlap: bool | None, why: str) -> None:
    write_health(world, AS_OF - timedelta(minutes=30), AS_OF - timedelta(minutes=30), sources=["prnewswire"],
                 result=result, overlap=overlap)
    res = fresh_catalyst(world, S, AS_OF)
    assert res["value"] == "unknown" and f"gap:prnewswire:{why}" in res["reasons"]


def test_a_gap_does_not_hide_a_true(world: Path) -> None:
    write_health(world, AS_OF - timedelta(minutes=30), AS_OF - timedelta(minutes=30), sources=["prnewswire"],
                 result="timeout")
    _facts(world, fact(S, published=AS_OF - timedelta(minutes=5), cls="trial"))
    assert fresh_catalyst(world, S, AS_OF)["value"] == "true"


@pytest.mark.parametrize("status", ["stale", "unavailable", "refused", "starting", "disabled"])
def test_a_source_not_ok_gives_unknown(scout_dir: Path, cfg: ScoutConfig, status: str) -> None:
    good_world(scout_dir, cfg, AS_OF)
    write_beats(scout_dir, AS_OF, AS_OF, sources={"edgar_atom": "ok", "globenewswire": status, "prnewswire": "ok"})
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(minutes=5), cls="fda"))
    res = fresh_catalyst(scout_dir, S, AS_OF)
    assert res["value"] == "unknown" and res["reasons"] == ["source_not_ok:globenewswire"]


def test_a_source_missing_from_the_beat_or_an_open_gap_gives_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    good_world(scout_dir, cfg, AS_OF)
    write_beats(scout_dir, AS_OF, AS_OF, sources={"edgar_atom": "ok", "prnewswire": "ok"})
    assert fresh_catalyst(scout_dir, S, AS_OF)["reasons"] == ["source_missing:globenewswire"]
    later = AS_OF + timedelta(seconds=10)
    write_beats(scout_dir, later, later, gap_open={"prnewswire": True})
    res = fresh_catalyst(scout_dir, S, later)
    assert res["value"] == "unknown" and "gap_open:prnewswire" in res["reasons"]


def test_no_gate6_sources_gives_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    good_world(scout_dir, cfg, AS_OF, params=False)
    write_params(scout_dir, cfg, AS_OF - timedelta(days=1), gate6_sources=[])
    assert fresh_catalyst(scout_dir, S, AS_OF)["reasons"] == ["no_gate6_sources"]


def test_stale_stopped_and_no_params(scout_dir: Path, cfg: ScoutConfig) -> None:
    res = fresh_catalyst(scout_dir, S, AS_OF)
    assert res["value"] == "unknown" and res["stale"] is True and "scout_stale" in res["reasons"]
    write_params(scout_dir, cfg, AS_OF - timedelta(days=1))
    write_beats(scout_dir, AS_OF - timedelta(seconds=200), AS_OF - timedelta(seconds=121))
    assert fresh_catalyst(scout_dir, S, AS_OF)["stale"] is True  # newest beat older than 120 s
    assert Reader(scout_dir, stale_s=300).fresh_catalyst(S, AS_OF)["stale"] is False
    write_beats(scout_dir, AS_OF, AS_OF, stopped=True)
    res = fresh_catalyst(scout_dir, S, AS_OF)
    assert res["stale"] is True and "scout_stopped" in res["reasons"]


def test_no_params_gives_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    good_world(scout_dir, cfg, AS_OF, params=False)
    res = fresh_catalyst(scout_dir, S, AS_OF)
    assert res["value"] == "unknown" and res["reasons"] == ["no_params"] and res["stale"] is False


def test_coverage_not_built_gives_unknown_not_false(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hide_module(monkeypatch, "coverage")
    assert fresh_catalyst(world, S, AS_OF)["reasons"] == ["not_built:coverage"]
    _facts(world, fact(S, published=AS_OF - timedelta(minutes=5), cls="fda"))
    assert fresh_catalyst(world, S, AS_OF)["value"] == "true"


def test_coverage_that_raises_gives_unknown(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> list:
        raise ZeroDivisionError("x")
    install_coverage(monkeypatch, boom)
    res = fresh_catalyst(world, S, AS_OF)
    assert res["value"] == "unknown" and res["reasons"] == ["error:ZeroDivisionError"]


def test_window_after_a_weekend_and_an_early_close(scout_dir: Path, cfg: ScoutConfig) -> None:
    # Monday 30 Nov 2026 09:00 ET: window from Friday 27 Nov 13:00 ET (early close after Thanksgiving)
    monday = et_at("2026-11-30", "09:00")
    early = et_at("2026-11-27", "13:00")
    good_world(scout_dir, cfg, monday)
    res = fresh_catalyst(scout_dir, S, monday)
    assert res["value"] == "false" and res["window"][0] == "2026-11-27T18:00:00Z"
    write_records(scout_dir, "facts", [fact(S, published=early - timedelta(minutes=1), cls="fda")])
    assert fresh_catalyst(scout_dir, S, monday)["value"] == "false"
    write_records(scout_dir, "facts", [fact(S, published=early + timedelta(hours=30), cls="contract")])  # Saturday
    assert fresh_catalyst(scout_dir, S, monday)["value"] == "true"


def test_news_headline_lists_classes(world: Path) -> None:
    _facts(world, fact(S, published=AS_OF - timedelta(minutes=9), cls="offering"),
           fact(S, published=AS_OF - timedelta(minutes=8), cls=None, kind="filing", title="8-K"),
           fact(S, published=AS_OF - timedelta(minutes=7), cls="fda"))
    head = news_headline(world, S, AS_OF)
    assert head["value"] == "true" and head["classes"] == ["None", "fda", "offering"] and len(head["evidence"]) == 3
    assert fresh_catalyst(world, S, AS_OF)["evidence"] == [head["evidence"][2]]
    assert news_headline(world, "NONE", AS_OF)["value"] == "false"


def test_labels_never_change_gate6(world: Path) -> None:
    before = fresh_catalyst(world, S, AS_OF)
    write_records(world, "labels", [{"labelled_at": "2026-11-04T00:00:00Z", "ticker": S, "catalyst_type": "fda",
                                     "status": "labelled", "verified": True, "job": "J1", "fact_id": "x"}])
    assert fresh_catalyst(world, S, AS_OF) == before


def test_answers_carry_no_future_as_of(world: Path) -> None:
    reader = Reader(world, now=lambda: AS_OF - timedelta(seconds=1))
    assert reader.fresh_catalyst(S, AS_OF)["reasons"] == ["future_as_of"]
    assert Reader(world, now=lambda: AS_OF).fresh_catalyst(S, AS_OF)["value"] == "false"


def test_utc_and_other_offsets_are_the_same_instant(world: Path) -> None:
    other = AS_OF.astimezone(timezone(timedelta(hours=10)))
    assert fresh_catalyst(world, S, other) == fresh_catalyst(world, S, AS_OF)
    assert isinstance(other, datetime)


def test_window_at_the_evening_check_starts_at_the_prior_trading_days_close(scout_dir: Path,
                                                                           cfg: ScoutConfig) -> None:
    """Regression: at Monday 19:40 ET the window started at Monday's own 16:00, so a catalyst
    released Monday before or during the session was invisible. SS 7.1: from the prior trading
    day's close (Friday's for a Monday), with Guardian's 20:00 ET rollover."""
    as_of = et_at("2026-11-02", "19:40")  # Monday
    good_world(scout_dir, cfg, as_of)
    _facts(scout_dir, fact(S, published=et_at("2026-11-02", "11:40"), cls="fda"))
    res = fresh_catalyst(scout_dir, S, as_of)
    assert res["value"] == "true" and res["window"][0] == "2026-10-30T20:00:00Z"  # Friday 16:00 EDT
    from scout_reader.gates import gate6_window
    assert gate6_window(et_at("2026-11-02", "20:30"))[0] == et_at("2026-11-02", "16:00")  # after the rollover
    assert gate6_window(et_at("2026-11-02", "09:00"))[0] == et_at("2026-10-30", "16:00")
    assert gate6_window(et_at("2026-11-28", "10:00"))[0] == et_at("2026-11-27", "13:00")  # Saturday after an early close
    assert gate6_window(et_at("2026-11-27", "19:45"))[0] == et_at("2026-11-25", "16:00")  # on the early-close day
