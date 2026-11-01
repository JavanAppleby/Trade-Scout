"""scout_reader.jason: the four Jason tags (SCOUT_SPEC 5.5, 9.1; DESIGN 3.8, D10). Stdlib only,
never raises, as-of rule on snapshots, every reading of an ambiguous level counts, no tag a gate."""

from __future__ import annotations

import ast
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from helpers_fakes import write_records
from helpers_levels import TODAY, one_setup, plan_with, read, write_read
from hypothesis import given, settings
from hypothesis import strategies as st

from scout import levels as jl
from scout.levels_snapshot import build_snapshot
from scout_reader import jason as rj

SEEN = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)  # 2026-09-15 21:00 ET
AS_OF = datetime(2026, 9, 16, 23, 45, tzinfo=timezone.utc)  # 2026-09-16 19:45 ET


def _snap(folder: Path, data: dict | list[dict], today: date = TODAY, seen: datetime = SEEN) -> dict:
    for item in data if isinstance(data, list) else [data]:
        write_read(folder, item)
    return build_snapshot(folder, today, seen).to_record()


@pytest.fixture()
def snap(tmp_path: Path) -> dict:
    return _snap(tmp_path / "levels", read())


def _values(tags: dict) -> dict:
    return {k: v["value"] for k, v in tags.items()}


# --------------------------------------------------------------------------- on_jason_list


def test_on_list_true_with_kind_and_direction(snap: dict) -> None:
    tags = rj.jason_tags_from(snap, "amd", 480, AS_OF)
    assert set(tags) == set(rj.TAGS)
    on = tags["on_jason_list"]
    assert on["value"] == "true" and on["source_kind"] == "tuesday" and on["direction"] == "avoid"
    assert on["evidence"] == ["jason:2026-09-15.tuesday:AMD"] and on["stale"] is False


def test_off_the_list_is_false_not_unknown(snap: dict) -> None:
    tags = rj.jason_tags_from(snap, "GRAB", 3.1, AS_OF)
    assert _values(tags) == {"on_jason_list": "false", "near_jason_level": "false", "jason_conflict": "false",
                             "jason_market_frame": "false"}
    assert tags["on_jason_list"]["reasons"] == ["not_on_jason_list"]


def test_several_current_reads_newest_first(tmp_path: Path) -> None:
    snap = _snap(tmp_path / "levels", [
        plan_with([one_setup("NVDA", [{"price": "100", "kind": "support"}])], session_date="2026-09-08",
                  expires_default="2026-09-30"),
        plan_with([one_setup("NVDA", [{"price": "200", "kind": "support"}], direction="short")],
                  session_date="2026-09-15", source_kind="weekly")])
    on = rj.jason_tags_from(snap, "NVDA", None, AS_OF)["on_jason_list"]
    assert on["direction"] == "short" and on["source_kind"] == "weekly"
    assert [s["session_date"] for s in on["setups"]] == ["2026-09-15", "2026-09-08"]


# --------------------------------------------------------------------------- as of


def test_no_snapshot_or_one_from_the_future_is_unknown(snap: dict) -> None:
    assert set(_values(rj.jason_tags_from(None, "AMD", 1, AS_OF)).values()) == {"unknown"}
    early = SEEN - timedelta(seconds=1)
    tags = rj.jason_tags_from(snap, "AMD", 1, early)
    assert set(_values(tags).values()) == {"unknown"}
    assert tags["on_jason_list"]["reasons"] == ["no_jason_snapshot"]


def test_the_latest_visible_snapshot_governs(tmp_path: Path, snap: dict) -> None:
    empty = build_snapshot(tmp_path / "empty", TODAY, SEEN + timedelta(hours=5)).to_record()
    records = [snap, empty]
    assert rj.jason_tags_from(records, "AMD", 1, SEEN + timedelta(hours=1))["on_jason_list"]["value"] == "true"
    assert rj.jason_tags_from(records, "AMD", 1, SEEN + timedelta(hours=6))["on_jason_list"]["value"] == "false"
    assert rj.latest_snapshot(records, SEEN - timedelta(1)) is None


def test_expiry_is_judged_on_as_ofs_et_date(snap: dict) -> None:
    last_et_evening = datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)  # 2026-09-19 22:00 ET
    assert rj.jason_tags_from(snap, "AMD", 1, last_et_evening)["on_jason_list"]["value"] == "true"
    next_day = datetime(2026, 9, 20, 4, 30, tzinfo=timezone.utc)  # 2026-09-20 00:30 ET
    assert rj.jason_tags_from(snap, "AMD", 1, next_day)["on_jason_list"]["value"] == "false"


def test_reads_the_file_and_ignores_a_partial_line(tmp_path: Path, snap: dict) -> None:
    scout_dir = tmp_path / "Scout"
    write_records(scout_dir, "jason", [snap], raw_lines=['{"format": 1, "first_seen_at": "2026-09-16T02:00:00Z"'])
    tags = rj.jason_tags_in(scout_dir, "NVDA", "215", AS_OF)
    assert tags["on_jason_list"]["value"] == "true"
    assert rj.jason_tags_in(tmp_path / "nobody", "NVDA", 1, AS_OF)["on_jason_list"]["reasons"] == ["no_jason_snapshot"]


def test_an_unknown_format_is_ignored(snap: dict) -> None:
    assert rj.jason_tags_from(dict(snap, format=2), "AMD", 1, AS_OF)["on_jason_list"]["value"] == "unknown"


# --------------------------------------------------------------------------- near_jason_level


def test_near_counts_every_reading_of_an_ambiguous_level(tmp_path: Path) -> None:
    snap = _snap(tmp_path / "levels", plan_with([one_setup("A", [
        {"price": "730", "kind": "support", "ambiguous": True, "price_alternatives": ["7.30"]},
        {"price": "20", "kind": "resistance"}])]))
    small = rj.jason_tags_from(snap, "A", "7.34", AS_OF)["near_jason_level"]
    assert small["value"] == "true" and small["ambiguous"] is True
    assert small["reasons"] == ["ambiguous_level: every reading counts"]
    (match,) = small["matches"]
    assert match["reading"] == "7.30" and match["readings"] == ["7.30", "730"] and match["kind"] == "support"
    big = rj.jason_tags_from(snap, "A", 725, AS_OF)["near_jason_level"]
    assert big["value"] == "true" and big["matches"][0]["reading"] == "730"
    plain = rj.jason_tags_from(snap, "A", "20.1", AS_OF)["near_jason_level"]
    assert plain["value"] == "true" and plain["ambiguous"] is False and plain["reasons"] == []
    far = rj.jason_tags_from(snap, "A", 15, AS_OF)["near_jason_level"]
    assert far["value"] == "false" and far["matches"] == []


def test_near_pct_is_the_callers(snap: dict) -> None:
    assert rj.jason_tags_from(snap, "NVDA", 216, AS_OF)["near_jason_level"]["value"] == "false"  # 1.4% from 213
    wide = rj.jason_tags_from(snap, "NVDA", 216, AS_OF, near_pct=0.02)["near_jason_level"]
    assert wide["value"] == "true" and wide["near_pct"] == 0.02
    assert rj.jason_tags_from(snap, "NVDA", 213.0, AS_OF)["near_jason_level"]["matches"][0]["distance_pct"] == 0.0


def test_near_without_a_price_is_unknown(snap: dict) -> None:
    assert rj.jason_tags_from(snap, "NVDA", None, AS_OF)["near_jason_level"]["reasons"] == ["no_price"]
    assert rj.jason_tags_from(snap, "NVDA", "abc", AS_OF)["near_jason_level"]["value"] == "unknown"
    assert rj.jason_tags_from(snap, "NVDA", -5, AS_OF)["near_jason_level"]["value"] == "unknown"


# --------------------------------------------------------------------------- jason_conflict


def test_conflict_reason_codes(snap: dict) -> None:
    amd = rj.jason_tags_from(snap, "AMD", 480, AS_OF)["jason_conflict"]
    assert amd["value"] == "true" and amd["codes"] == ["jason_avoid", "below_jason_invalidation"]
    assert amd["level"] == {"kind": "invalidation", "price": "500"}
    nvda = rj.jason_tags_from(snap, "NVDA", 185, AS_OF)["jason_conflict"]
    assert nvda["codes"] == ["below_jason_invalidation"]
    calm = rj.jason_tags_from(snap, "NVDA", 215, AS_OF)["jason_conflict"]
    assert calm["value"] == "false" and calm["codes"] == []
    igv = rj.jason_tags_from(snap, "IGV", 99, AS_OF)["jason_conflict"]
    assert igv["codes"] == ["below_jason_support"]
    assert rj.jason_tags_from(snap, "AMD", None, AS_OF)["jason_conflict"]["codes"] == ["jason_avoid"]
    assert rj.jason_tags_from(snap, "NVDA", None, AS_OF)["jason_conflict"]["reasons"] == ["no_price"]


@pytest.mark.parametrize("symbol", ["NVDA", "AMD", "TSLA", "IGV", "SPY"])
@pytest.mark.parametrize("price", ["1", "99", "185", "189.99", "190", "212", "215", "380", "450", "499", "520"])
def test_conflict_agrees_with_the_ported_conflict_for(tmp_path: Path, symbol: str, price: str) -> None:
    folder = tmp_path / "levels"
    write_read(folder, read())
    plan = jl.load_folder(folder, TODAY)
    record = jl.conflict_for(symbol, price, plan=plan, today=TODAY)
    snap = build_snapshot(folder, TODAY, SEEN).to_record()
    tag = rj.jason_tags_from(snap, symbol, price, AS_OF)["jason_conflict"]
    assert tag["codes"] == (record["jason_conflict_reasons"] if record else [])
    if record and record.get("jason_level") and tag["level"]:
        assert tag["level"]["price"] == record["jason_level"]


# --------------------------------------------------------------------------- jason_market_frame


def test_market_frame_lists_levels_and_needs_prices_for_above_below(tmp_path: Path) -> None:
    folder = tmp_path / "levels"
    snap = _snap(folder, [
        plan_with([one_setup("SPY", [{"price": "750", "kind": "support"}, {"price": "775", "kind": "resistance"},
                                     {"price": "730", "kind": "support", "ambiguous": True,
                                      "price_alternatives": ["7.30"]}], direction="neutral"),
                   one_setup("QQQ", [{"price": "699", "kind": "support"}], direction="neutral")]),
        plan_with([one_setup("SPY", [{"price": "760", "kind": "support"}])], source_kind="daily",
                  session_date="2026-09-16")], today=TODAY)
    frame = rj.jason_tags_from(snap, "XYZ", 3, AS_OF)["jason_market_frame"]
    assert frame["value"] == "true"
    assert frame["frames"]["SPY"]["source_kind"] == "tuesday"  # the Tuesday read, not the newer daily one
    assert "position" not in frame["frames"]["SPY"]["levels"][0]
    assert "no_frame_prices: levels listed without above/below" in frame["reasons"]
    priced = rj.jason_tags_from(snap, "XYZ", 3, AS_OF, frame_prices={"spy": 760, "QQQ": "690"})["jason_market_frame"]
    spy, qqq = priced["frames"]["SPY"], priced["frames"]["QQQ"]
    assert [lvl["position"] for lvl in spy["levels"]] == ["above", "below", "above"]
    between = rj.jason_tags_from(snap, "X", 3, AS_OF, frame_prices={"SPY": 500})["jason_market_frame"]
    assert [lvl["position"] for lvl in between["frames"]["SPY"]["levels"]] == ["below", "below", "inside"]
    at = rj.jason_tags_from(snap, "X", 3, AS_OF, frame_prices={"SPY": "750"})["jason_market_frame"]
    assert at["frames"]["SPY"]["levels"][0]["position"] == "at"
    assert (spy["above"], spy["below"], spy["price"]) == (2, 1, "760")
    assert [lvl["position"] for lvl in qqq["levels"]] == ["below"]
    assert priced["reasons"] == []


def test_market_symbols_are_configurable_and_absent_frame_is_false(snap: dict) -> None:
    assert rj.jason_tags_from(snap, "X", 1, AS_OF)["jason_market_frame"]["value"] == "false"
    custom = rj.jason_tags_from(snap, "X", 1, AS_OF, market_symbols=["NVDA"])["jason_market_frame"]
    assert custom["value"] == "true" and list(custom["frames"]) == ["NVDA"]


# --------------------------------------------------------------------------- never raises, never a gate


def test_naive_as_of_and_stale(snap: dict) -> None:
    naive = rj.jason_tags_from(snap, "AMD", 1, datetime(2026, 9, 16, 12, 0))
    assert {v["reasons"][0] for v in naive.values()} == {"naive_as_of"}
    stale = rj.jason_tags_from(snap, "AMD", 1, AS_OF, stale=True)
    assert all(v["value"] == "unknown" and v["stale"] is True and v["reasons"] == ["scout_stale"]
               for v in stale.values())


@pytest.mark.parametrize("bad", [
    {"format": 1, "first_seen_at": "2026-09-16T00:00:00Z", "setups": "nope"},
    {"format": 1, "first_seen_at": "2026-09-16T00:00:00Z", "setups": [None, 3, {"symbol": "AMD", "expires": 5}]},
    {"format": 1, "first_seen_at": "2026-09-16T00:00:00Z", "setups": [
        {"symbol": "AMD", "expires": "2026-12-31", "levels": [None, {"kind": "support", "readings": ["x", None, 7]}]}]},
    {"format": True, "first_seen_at": "2026-09-16T00:00:00Z"},
    {"format": 1, "first_seen_at": 12},
    ["not a record", 3, None],
    object(),
])
def test_corrupt_snapshots_never_raise(bad: object) -> None:
    tags = rj.jason_tags_from(bad, "AMD", "1", AS_OF, frame_prices={"SPY": "x"})  # type: ignore[arg-type]
    assert set(tags) == set(rj.TAGS)
    assert all(v["value"] in ("true", "false", "unknown") for v in tags.values())


_json = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=8),
                     lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
                     max_leaves=20)


@settings(max_examples=150, deadline=None)
@given(setups=st.lists(st.dictionaries(st.sampled_from(["symbol", "expires", "levels", "direction", "session_date",
                                                         "source_kind", "kind", "readings"]), _json, max_size=6),
                       max_size=4),
       price=_json, near=_json)
def test_fuzzed_snapshots_never_raise(setups: list, price: object, near: object) -> None:
    rec = {"format": 1, "first_seen_at": "2026-09-16T00:00:00Z", "setups": setups}
    tags = rj.jason_tags_from(rec, "AMD", price, AS_OF, near if isinstance(near, (int, float)) else None,
                              frame_prices={"SPY": price})
    assert all(v["value"] in ("true", "false", "unknown") for v in tags.values())


def test_an_error_becomes_unknown(monkeypatch: pytest.MonkeyPatch, snap: dict) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise ZeroDivisionError

    monkeypatch.setattr(rj, "current_for", boom)
    tags = rj.jason_tags_from(snap, "AMD", 1, AS_OF)
    assert {v["reasons"][0] for v in tags.values()} == {"error:ZeroDivisionError"}


def test_values_are_tri_state_and_decimal_free(snap: dict) -> None:
    import json

    tags = rj.jason_tags_from(snap, "NVDA", Decimal("213.5"), AS_OF, frame_prices={"SPY": 1})
    json.dumps(tags)  # plain JSON values only (Radar freezes them with the find)
    assert all(v["value"] in ("true", "false", "unknown") for v in tags.values())


def test_the_reader_module_is_stdlib_only() -> None:
    source = Path(rj.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"scout_reader", "__future__"}
    assert roots <= allowed, roots - allowed
