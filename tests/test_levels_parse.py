"""The price grammar ported from the old platform (SCOUT_SPEC 5.3).

Ported from `tests/test_phase_ao_jason_levels.py` (ai-trading-platform d62a282): AO3 and AO4 (the
grammar) plus direct cases for each public function of `scout.levels_parse`."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scout import levels_parse as lp

D = Decimal


@pytest.mark.parametrize("token,expected", [
    ("213", "213"), ("$70", "70"), ("70K", "70000"), ("70k", "70000"), ("2,085.84", "2085.84"),
    ("$1,234,567", "1234567"), ("7.30", "7.30"), ("[6]5", "65"), (" 42 ", "42"), ("2.5K", "2500.0"),
])
def test_one_price_reads_what_is_written(token: str, expected: str) -> None:
    assert lp.one_price(token) == D(expected)


@pytest.mark.parametrize("token", ["330s", "70s", "64 30", "$2,85.84", "over 200", "", "0", "-5", "abc", "1,00"])
def test_one_price_refuses_what_needs_interpreting(token: str) -> None:
    assert lp.one_price(token) is None


# AO3: ranges, pairs and thousands are mechanical and are read; a zone is ambiguous.
@pytest.mark.parametrize("written,expected", [
    ("213", ["213"]),
    ("$70", ["70"]),
    ("70K", ["70000"]),
    ("2,085.84", ["2085.84"]),
    ("155-156", ["155", "156"]),
    ("130 to 125", ["130", "125"]),
    ("325, 320", ["325", "320"]),
    ("6[5]-$70", ["65", "70"]),
])
def test_AO3_prices_in_reads_ranges_and_pairs(written: str, expected: list[str]) -> None:
    readings, zone, problem = lp.prices_in(written)
    assert [str(r) for r in readings] == expected
    assert problem is None
    assert zone is (len(expected) > 1)


# AO4: a decade, a gap or prose is refused and the warning names the fix.
@pytest.mark.parametrize("written", ["330s", "70s", "64 30", "$2,85.84", "a 400 handle reference", "over 200"])
def test_AO4_prices_in_refuses_and_names_the_fix(written: str) -> None:
    readings, zone, problem = lp.prices_in(written)
    assert readings == [] and zone is False
    assert problem is not None and "price_alternatives" in problem


def test_prices_in_empty_and_none() -> None:
    assert lp.prices_in("") == ([], False, "empty price")
    assert lp.prices_in(None) == ([], False, "empty price")


def test_level_readings_merges_alternatives_sorted_and_unique() -> None:
    readings, ambiguous, problems = lp.level_readings({"price": "730", "price_alternatives": ["7.30", "730"]})
    assert [str(r) for r in readings] == ["7.30", "730"]
    assert ambiguous is True and problems == []


def test_level_readings_flag_without_alternatives_is_still_ambiguous() -> None:
    readings, ambiguous, _ = lp.level_readings({"price": "50", "ambiguous": True})
    assert readings == [D("50")] and ambiguous is True


def test_level_readings_alternatives_rescue_an_unreadable_primary() -> None:
    readings, ambiguous, problems = lp.level_readings(
        {"price": "2K, 21, $2,85.84", "price_alternatives": ["2000", "2100-2200", "2085.84"]})
    assert [str(r) for r in readings] == ["2000", "2085.84", "2100", "2200"]
    assert ambiguous is True
    assert any("$2,85.84" in p for p in problems)


def test_level_readings_bad_alternatives_field() -> None:
    _, _, problems = lp.level_readings({"price": "10", "price_alternatives": "11"})
    assert problems == ["price_alternatives is not a list"]
    _, _, problems = lp.level_readings({"price": "10", "price_alternatives": ["10s"]})
    assert problems and "price_alternative '10s'" in problems[0]


def test_parse_levels_expands_one_level_per_reading_with_position() -> None:
    levels, problems = lp.parse_levels("X", [
        {"price": "155-156", "kind": "Support", "note": "zone"},
        {"price": "156", "kind": "support"},  # the same reading again is one level
        {"price": "10", "kind": "pivot"},
        "not an object",
        {"price": "", "kind": "target"},
        {"kind": "trigger", "price": "330s"},
    ])
    assert [(lvl["level_id"], lvl["position"]) for lvl in levels] == [("support@155", 0), ("support@156", 0)]
    assert all(lvl["ambiguous"] and lvl["alternatives"] == ["155", "156"] for lvl in levels)
    assert any("kind 'pivot'" in p for p in problems)
    assert any("levels[3] is not an object" in p for p in problems)
    assert any("levels[4] empty price" in p for p in problems)
    assert sum("levels[5]" in p for p in problems) == 1  # one complaint per dropped level


def test_parse_levels_not_a_list() -> None:
    assert lp.parse_levels("X", {"price": "1"}) == ([], ["X: levels is not a list"])


def test_parse_conditionals_are_carried_verbatim() -> None:
    out, problems = lp.parse_conditionals("X", [{"if": " lose 750 ", "then": "730"}, 3])
    assert out == [{"if": "lose 750", "then": "730"}]
    assert problems == ["X: conditionals[1] is not an object"]
    assert lp.parse_conditionals("X", None) == ([], [])
    assert lp.parse_conditionals("X", "prose") == ([], ["X: conditionals is not a list"])


def test_parse_setup_defaults_and_born_stale() -> None:
    setup, problems = lp.parse_setup({"symbol": "abc", "direction": "LONGG", "attribution": "regan",
                                      "expires": "next week", "levels": []},
                                     session_date="2026-09-15", default_expires="2026-09-22", source_kind="daily")
    assert setup is not None
    assert setup["symbol"] == "ABC" and setup["direction"] == "neutral" and setup["attribution"] == "unclear"
    assert setup["expires"] == "2026-09-22" and setup["source_kind"] == "daily"
    assert len(problems) == 3
    stale, problems = lp.parse_setup({"symbol": "A", "expires": "2026-09-01"}, session_date="2026-09-15",
                                     default_expires="2026-09-22")
    assert stale is not None and any("born stale" in p for p in problems)


def test_parse_setup_drops_what_has_no_symbol() -> None:
    assert lp.parse_setup([], session_date="2026-09-15", default_expires="x")[0] is None
    none, problems = lp.parse_setup({"symbol": " "}, session_date="2026-09-15", default_expires="x")
    assert none is None and problems == ["a setup without a symbol - dropped"]


def test_small_readers() -> None:
    assert lp.dec("1.5") == D("1.5") and lp.dec("0") is None and lp.dec("nan") is None and lp.dec(None) is None
    assert lp.iso_date("2026-09-15 ") == "2026-09-15" and lp.iso_date("15/09/2026") is None
    assert lp.headline({"stream_title": "Amit", "host": "x"}) == "Amit"
    assert lp.headline({"other": 1}) == "" and lp.headline(" SPY ") == "SPY"
