"""The level-file loader, the folder merge and the queries (SCOUT_SPEC 5.1, 5.3).

Ported from `tests/test_phase_ao_jason_levels.py` (ai-trading-platform d62a282, 64 tests). Kept:
AO1-AO9 (the schema reader; AO9 now reads the copied live read instead of the platform's
`config/jason_levels.json`), the loader half of AO17, AO36-AO38 (the conflict record), and the
scorer cases AO50-AO53 (in tests/test_levels_score.py). Dropped because the feature is not in
Scout: AO10-AO16 and AO18-AO26 (the weekly-watch state machine, alerts, ledger rows, quote feed),
AO27-AO35 (the demo trade feed), AO39-AO49 (signal stamping and the buy-the-dip gate), AO54-AO64
(the watch's archive, supervisor, eToro resolver, price-mismatch and re-entry machine), and AO6's
`feedable_setups` assertion (Scout feeds no trade path).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from helpers_levels import FIXTURE_20260915, FIXTURE_LIVE, TODAY, one_setup, plan_with, read, write_read

from scout import levels as jl


@pytest.fixture()
def read_path(tmp_path: Path) -> Path:
    return write_read(tmp_path, read(), "jason_levels.json")


# --------------------------------------------------------------------------- AO1-AO9: the reader


def test_AO1_the_schema_loads_and_expands_a_read(read_path: Path) -> None:
    plan = jl.load_plan(read_path)
    assert plan["session_date"] == "2026-09-15"
    assert plan["accept_minutes"] == 15
    assert plan["source_kind"] == "tuesday"
    assert [s["symbol"] for s in plan["setups"]] == ["NVDA", "AMD", "TSLA", "IGV"]
    assert plan["warnings"] == []
    nvda = jl.setup_for("NVDA", plan, TODAY)
    assert {lvl["kind"] for lvl in nvda["levels"]} == {"support", "trigger", "resistance", "invalidation", "target"}
    assert nvda["conditionals"] == [{"if": "look above and fail", "then": "kind of lame"}]
    assert nvda["source_kind"] == "tuesday" and nvda["session_date"] == "2026-09-15"


def test_AO2_a_missing_session_date_raises_but_a_bad_setup_does_not(tmp_path: Path) -> None:
    undated = write_read(tmp_path, {"setups": []}, "undated.json")
    with pytest.raises(jl.JasonPlanError):
        jl.load_plan(undated)
    partly_bad = write_read(tmp_path, plan_with([
        one_setup("NVDA", [{"price": "220", "kind": "trigger"}, {"price": "330s", "kind": "support"}]),
        one_setup("OK", [{"price": "10", "kind": "trigger"}]),
        {"direction": "long"},
    ]), "partly.json")
    plan = jl.load_plan(partly_bad)
    assert [s["symbol"] for s in plan["setups"]] == ["NVDA", "OK"]
    assert len(jl.setup_for("NVDA", plan, TODAY)["levels"]) == 1
    assert any("330s" in w for w in plan["warnings"])
    assert any("without a symbol" in w for w in plan["warnings"])


@pytest.mark.parametrize("written,expected", [
    ("213", ["213"]), ("$70", ["70"]), ("70K", ["70000"]), ("2,085.84", ["2085.84"]), ("155-156", ["155", "156"]),
    ("130 to 125", ["125", "130"]), ("325, 320", ["320", "325"]), ("6[5]-$70", ["65", "70"]),
])
def test_AO3_a_price_written_out_as_numbers_is_parsed(written: str, expected: list[str], tmp_path: Path) -> None:
    path = write_read(tmp_path, plan_with([one_setup("X", [{"price": written, "kind": "support"}])]))
    plan = jl.load_plan(path)
    levels = jl.setup_for("X", plan, TODAY)["levels"]
    assert [str(lvl["price"]) for lvl in levels] == expected
    assert plan["warnings"] == []
    assert all(lvl["ambiguous"] for lvl in levels) is (len(expected) > 1)


@pytest.mark.parametrize("written", ["330s", "70s", "64 30", "$2,85.84", "a 400 handle reference", "over 200"])
def test_AO4_a_price_that_would_need_interpreting_is_refused_and_named(written: str, tmp_path: Path) -> None:
    path = write_read(tmp_path, plan_with([one_setup("X", [{"price": written, "kind": "support"}])]))
    plan = jl.load_plan(path)
    assert jl.setup_for("X", plan, TODAY)["levels"] == []
    assert any("price_alternatives" in w for w in plan["warnings"])


def test_AO5_an_unreadable_price_still_uses_the_transcribers_alternatives(tmp_path: Path) -> None:
    path = write_read(tmp_path, plan_with([one_setup("ETH", [
        {"price": "2K, 21, $2,85.84", "kind": "support", "ambiguous": True,
         "price_alternatives": ["2000", "2100-2200", "2085.84"]}])]))
    plan = jl.load_plan(path)
    levels = jl.setup_for("ETH", plan, TODAY)["levels"]
    assert [str(lvl["price"]) for lvl in levels] == ["2000", "2085.84", "2100", "2200"]
    assert all(lvl["ambiguous"] for lvl in levels)
    assert any("$2,85.84" in w for w in plan["warnings"])


def test_AO6_an_unknown_direction_or_attribution_degrades(tmp_path: Path) -> None:
    path = write_read(tmp_path, plan_with([
        one_setup("A", [{"price": "10", "kind": "trigger"}], direction="LONGG"),
        {"symbol": "B", "direction": "long", "levels": [{"price": "10", "kind": "trigger"}]}]))
    plan = jl.load_plan(path)
    assert jl.setup_for("A", plan, TODAY)["direction"] == "neutral"
    assert jl.setup_for("B", plan, TODAY)["attribution"] == "unclear"


def test_AO7_an_expired_setup_is_not_current(read_path: Path) -> None:
    plan = jl.load_plan(read_path)
    assert jl.plan_is_current(plan, date(2026, 9, 19))
    assert not jl.plan_is_current(plan, date(2026, 9, 20))
    assert jl.setup_for("NVDA", plan, date(2026, 9, 20)) is None
    assert jl.setup_for("NVDA", plan, "2026-09-19") is not None  # an ISO string works too


def test_AO8_the_file_sets_its_own_default_horizon(tmp_path: Path) -> None:
    path = write_read(tmp_path, plan_with([one_setup("A", [{"price": "10", "kind": "trigger"}])],
                                          expires_default="2026-09-30"))
    plan = jl.load_plan(path)
    assert plan["setups"][0]["expires"] == "2026-09-30"
    assert plan["expires_default"] == "2026-09-30"


def test_AO8b_without_expires_default_a_read_lives_seven_days(tmp_path: Path) -> None:
    plan = jl.load_plan(write_read(tmp_path, plan_with([one_setup("A", [{"price": "1", "kind": "support"}])])))
    assert plan["setups"][0]["expires"] == "2026-09-22"
    bad = jl.load_plan(write_read(tmp_path, plan_with([one_setup("A", [])], expires_default="soon"), "b.json"))
    assert bad["setups"][0]["expires"] == "2026-09-22"
    assert any("expires_default 'soon'" in w for w in bad["warnings"])


def test_AO9_the_live_read_parses_and_every_warning_is_a_sentence() -> None:
    """The 2026-09-15 read itself (76 setups, 206 declared levels; SCOUT_SPEC 5.1)."""
    raw = json.loads(FIXTURE_LIVE.read_text(encoding="utf-8"))
    assert len(raw["setups"]) == 76
    assert sum(len(s.get("levels", ())) for s in raw["setups"]) == 206
    plan = jl.load_plan(FIXTURE_LIVE)
    assert plan["session_date"] == "2026-09-15"
    assert len(plan["setups"]) >= 70  # a duplicate symbol or a setup without one may drop
    assert plan["source_kind"] == "tuesday"
    assert plan["source"]  # a block, read through its headline key
    assert any("source_kind is missing" in w for w in plan["warnings"])
    for warning in plan["warnings"]:
        assert ":" in warning or "source_kind" in warning
        assert len(warning) > 20
    eth = next(s for s in plan["setups"] if s["symbol"] == "ETH")
    assert any(lvl["ambiguous"] for lvl in eth["levels"])


def test_the_old_fixture_parses_cleanly_apart_from_source_kind() -> None:
    plan = jl.load_plan(FIXTURE_20260915)
    assert [s["symbol"] for s in plan["setups"]][:3] == ["SPY", "QQQ", "NVDA"]
    assert plan["warnings"] == ["source_kind is missing - read as 'tuesday'"]
    spy = jl.setup_for("SPY", plan, TODAY)
    assert [str(lvl["price"]) for lvl in jl.levels_of(spy, "support")] == ["750", "7.30", "730", "700"]


def test_the_fixtures_are_byte_identical_to_their_sources() -> None:
    assert hashlib.sha256(FIXTURE_20260915.read_bytes()).hexdigest() == \
        "a4eb24d92a88afc992a11ccb0aaa39f672f314cc58444195a1591c5e96cba7cb"
    assert hashlib.sha256(FIXTURE_LIVE.read_bytes()).hexdigest() == \
        "da92ee49f9a0eab89fa0c051a9c49b1181121bbf21d879dc08edba73a4e8e1a7"


def test_AO17_an_ambiguous_level_expands_to_every_reading(tmp_path: Path) -> None:
    path = write_read(tmp_path, plan_with([one_setup("A", [
        {"price": "730", "kind": "support", "ambiguous": True, "price_alternatives": ["7.30"]}])]))
    plan = jl.load_plan(path)
    assert [str(lvl["price"]) for lvl in plan["setups"][0]["levels"]] == ["7.30", "730"]
    assert all(lvl["alternatives"] == ["7.30", "730"] for lvl in plan["setups"][0]["levels"])


# --------------------------------------------------------------------------- top level and bad files


@pytest.mark.parametrize("body,needle", [
    ("not json", "unreadable"), ("[1, 2]", "top level must be an object"),
    (json.dumps({"session_date": "Tuesday", "setups": []}), "session_date"),
    (json.dumps({"session_date": "2026-09-15", "setups": {}}), "setups must be a list"),
])
def test_a_bad_file_raises(tmp_path: Path, body: str, needle: str) -> None:
    with pytest.raises(jl.JasonPlanError, match=needle):
        jl.load_plan(write_read(tmp_path, body, "bad.json"))


def test_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(jl.JasonPlanError, match="does not exist"):
        jl.load_plan(tmp_path / "absent.json")


def test_source_kind_unknown_keys_and_accept_minutes(tmp_path: Path) -> None:
    plan = jl.load_plan(write_read(tmp_path, plan_with([], source_kind="Weekly", accept_minutes="x",
                                                        new_top_level_key={"anything": 1})))
    assert plan["source_kind"] == "weekly"
    assert plan["accept_minutes"] == 15 and any("accept_minutes" in w for w in plan["warnings"])
    odd = jl.load_plan(write_read(tmp_path, plan_with([], source_kind="monthly"), "o.json"))
    assert odd["source_kind"] == "tuesday" and any("'monthly'" in w for w in odd["warnings"])


def test_a_symbol_declared_twice_last_wins(tmp_path: Path) -> None:
    plan = jl.load_plan(write_read(tmp_path, plan_with([
        one_setup("A", [{"price": "1", "kind": "support"}]), one_setup("a", [{"price": "2", "kind": "support"}])])))
    assert len(plan["setups"]) == 1 and str(plan["setups"][0]["levels"][0]["price"]) == "2"
    assert any("declared more than once" in w for w in plan["warnings"])


def test_a_utf8_bom_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(read()).encode("utf-8"))
    assert jl.load_plan(path)["session_date"] == "2026-09-15"


# --------------------------------------------------------------------------- AO36-AO38: conflict record


def test_AO36_conflict_fires_on_short_avoid_and_below_a_level(read_path: Path) -> None:
    plan = jl.load_plan(read_path)
    avoid = jl.conflict_for("AMD", "480", plan=plan, today=TODAY)
    assert avoid["jason_conflict_reasons"] == ["jason_avoid", "below_jason_invalidation"]
    assert avoid["jason_level"] == "500"
    assert avoid["jason_source_kind"] == "tuesday"
    assert jl.conflict_for("NVDA", "185", plan=plan, today=TODAY)["jason_conflict"] == "below_jason_invalidation"
    assert jl.conflict_for("NVDA", "215", plan=plan, today=TODAY) is None
    assert jl.conflict_for("NOT_IN_THE_READ", "1", plan=plan, today=TODAY) is None
    no_price = jl.conflict_for("AMD", None, plan=plan, today=TODAY)
    assert no_price["jason_conflict_reasons"] == ["jason_avoid"] and no_price["jason_price_seen"] is None


def test_AO37_a_support_only_read_still_flags_below_every_support(tmp_path: Path) -> None:
    plan = jl.load_plan(write_read(tmp_path, plan_with([one_setup("A", [
        {"price": "213", "kind": "support"}, {"price": "190", "kind": "support"}])])))
    assert jl.conflict_for("A", "200", plan=plan, today=TODAY) is None
    flag = jl.conflict_for("A", "185", plan=plan, today=TODAY)
    assert flag["jason_conflict"] == "below_jason_support"
    assert flag["jason_level"] == "190"


def test_AO38_no_read_is_never_a_conflict(tmp_path: Path) -> None:
    assert jl.conflict_for("NVDA", "1", path=tmp_path / "absent.json", today=TODAY) is None
    assert jl.invalidation_reference("NVDA", path=tmp_path / "absent.json", today=TODAY) is None
    assert jl.conflict_for("NVDA", "1", today=TODAY) is None


def test_invalidation_reference_takes_the_lowest(read_path: Path, tmp_path: Path) -> None:
    plan = jl.load_plan(read_path)
    ref = jl.invalidation_reference("NVDA", plan=plan, today=TODAY)
    assert ref["jason_invalidation"] == "190" and ref["jason_invalidation_kind"] == "invalidation"
    igv = jl.invalidation_reference("IGV", plan=plan, today=TODAY)
    assert igv["jason_invalidation"] == "100" and igv["jason_invalidation_kind"] == "support"
    bare = jl.load_plan(write_read(tmp_path, plan_with([one_setup("Z", [{"price": "5", "kind": "target"}])]), "z.json"))
    assert jl.invalidation_reference("Z", plan=bare, today=TODAY) is None


def test_symbols_and_describe(read_path: Path) -> None:
    plan = jl.load_plan(read_path)
    assert jl.symbols(plan, TODAY) == ["AMD", "IGV", "NVDA", "TSLA"]
    assert jl.symbols(plan, date(2026, 9, 25)) == []
    nvda = jl.setup_for("NVDA", plan, TODAY)
    assert jl.describe_levels(jl.levels_of(nvda, "support")) == "support 213"
    assert jl.describe_levels([]) == "no levels"
    assert "(ambiguous)" in jl.describe_levels([{"kind": "support", "price": 1, "ambiguous": True}])


# --------------------------------------------------------------------------- load_folder


def test_load_folder_merges_current_files_newest_first(tmp_path: Path) -> None:
    folder = tmp_path / "levels"
    write_read(folder, plan_with([one_setup("NVDA", [{"price": "100", "kind": "support"}]),
                                  one_setup("OLD", [{"price": "5", "kind": "support"}])],
                                 session_date="2026-09-08", expires_default="2026-09-20"))
    write_read(folder, plan_with([one_setup("NVDA", [{"price": "200", "kind": "support"}], direction="short")],
                                 session_date="2026-09-15", source_kind="weekly"))
    write_read(folder, plan_with([one_setup("NVDA", [{"price": "210", "kind": "support"}])],
                                 session_date="2026-09-15", source_kind="daily", expires_default="2026-09-15"))
    write_read(folder, plan_with([one_setup("GONE", [])], session_date="2026-09-01"))  # expired by TODAY
    write_read(folder, "garbage", "2026-09-14.tuesday.json")
    (folder / "2026-09-15.weekly.meta.json").write_text("{}", encoding="utf-8")
    plan = jl.load_folder(folder, TODAY)
    assert plan["files"] == ["2026-09-08.tuesday.json", "2026-09-15.weekly.json"]
    nvda = [s for s in plan["setups"] if s["symbol"] == "NVDA"]
    assert [(s["session_date"], s["source_kind"], s["file"]) for s in nvda] == [
        ("2026-09-15", "weekly", "2026-09-15.weekly.json"), ("2026-09-08", "tuesday", "2026-09-08.tuesday.json")]
    assert jl.setup_for("NVDA", plan, TODAY)["direction"] == "short"
    assert jl.symbols(plan, TODAY) == ["NVDA", "OLD"]
    assert any(w.startswith("2026-09-14.tuesday.json: refused") for w in plan["warnings"])
    # the daily file was current on its own session date only
    on_15 = jl.load_folder(folder, date(2026, 9, 15))
    first = [s for s in on_15["setups"] if s["symbol"] == "NVDA"][0]
    assert first["source_kind"] == "daily"


def test_load_folder_missing_folder_is_empty(tmp_path: Path) -> None:
    assert jl.load_folder(tmp_path / "nope", TODAY) == {"today": "2026-09-16", "files": [], "setups": [],
                                                        "warnings": []}
