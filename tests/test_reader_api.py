"""scout_reader.api: shape, never raises, fail closed, delegation (SCOUT_SPEC 2 rule 5, 10.3, 14;
DESIGN 3.1-3.2). Includes the fail-closed property tests."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import scout_reader
from scout_reader import api, gates
from scout_reader.api import JASON_TAGS, MARKET_FIELDS, Reader, hold_allowed

from scout.clock import iso
from scout.config import ScoutConfig
from scout.paths import ScoutPaths

from conftest import et_at, make_config
from helpers_answers import (
    due_record, fact, good_world, hide_module, install_all_stubs, install_module, params_record, tri, write_beats,
    write_records,
)

AS_OF = et_at("2026-11-03", "19:45")
S = "ACME"
DAY = "2026-11-03.jsonl"
PROPERTY = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    install_all_stubs(monkeypatch)


def _world(root: Path, cfg: ScoutConfig, *, due: bool = True) -> Path:
    good_world(root, cfg, AS_OF)
    if due:
        write_records(root, "due", [due_record(S, AS_OF - timedelta(minutes=1), due_start="2027-01-10",
                                               due_end="2027-04-05")])
    return root


def _six(root: Path, as_of: Any = AS_OF, symbol: Any = S) -> dict[str, Any]:
    r = Reader(root)
    return {"news_headline": r.news_headline(symbol, as_of), "fresh_catalyst": r.fresh_catalyst(symbol, as_of),
            "event_before_next_session": r.event_before_next_session(symbol, as_of),
            "halt_open": r.halt_open(symbol, as_of), **{f"jason.{k}": v for k, v in
                                                         r.jason_tags(symbol, 1.0, as_of).items()},
            **{f"market.{k}": v for k, v in r.market_context(as_of).items()}}


def _values(answers: dict[str, Any]) -> set[str]:
    return {v["value"] for v in answers.values()}


# --------------------------------------------------------------------------- shape


def test_shape_of_every_answer(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    answers = _six(scout_dir)
    for name, res in answers.items():
        assert set(res) >= {"value", "evidence", "stale", "reasons"}, name
        assert res["value"] in ("true", "false", "unknown") and isinstance(res["evidence"], list)
        assert isinstance(res["reasons"], list) and isinstance(res["stale"], bool)
    assert set(Reader(scout_dir).jason_tags(S, None, AS_OF)) == set(JASON_TAGS)
    assert set(Reader(scout_dir).market_context(AS_OF)) == set(MARKET_FIELDS)
    assert answers["fresh_catalyst"]["value"] == "false" and answers["event_before_next_session"]["value"] == "false"
    assert "classes" in answers["news_headline"]


def test_module_functions_and_the_package_reexport(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    r = Reader(scout_dir)
    assert api.fresh_catalyst(scout_dir, S, AS_OF) == r.fresh_catalyst(S, AS_OF)
    assert api.news_headline(str(scout_dir), S, AS_OF) == r.news_headline(S, AS_OF)
    assert api.event_before_next_session(scout_dir, S, AS_OF) == r.event_before_next_session(S, AS_OF)
    assert api.halt_open(scout_dir, S, AS_OF) == r.halt_open(S, AS_OF)
    assert api.jason_tags(scout_dir, S, 2.0, AS_OF, near_pct=0.05) == r.jason_tags(S, 2.0, AS_OF, near_pct=0.05)
    assert api.market_context(scout_dir, AS_OF) == r.market_context(AS_OF)
    assert scout_reader.fresh_catalyst is api.fresh_catalyst and scout_reader.Reader is Reader


def test_reader_imports_only_the_standard_library() -> None:
    import ast
    import sys
    stdlib = set(sys.stdlib_module_names)
    for name in ("api.py", "gates.py", "datetext.py"):
        tree = ast.parse((Path(api.__file__).parent / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for mod in mods:
                top = mod.split(".")[0]
                assert top in stdlib or top in ("scout_reader", "__future__"), f"{name} imports {mod}"


# --------------------------------------------------------------------------- refusals and errors


def test_naive_as_of_is_refused_everywhere(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    for name, res in _six(scout_dir, as_of=AS_OF.replace(tzinfo=None)).items():
        assert res["value"] == "unknown" and res["reasons"] == ["naive_as_of"], name
    assert api.fresh_catalyst(scout_dir, S, "2026-11-03")["reasons"] == ["naive_as_of"]  # type: ignore[arg-type]


@pytest.mark.parametrize("symbol", [None, "", "   ", 5])
def test_bad_symbol(scout_dir: Path, cfg: ScoutConfig, symbol: Any) -> None:
    _world(scout_dir, cfg)
    for name, res in _six(scout_dir, symbol=symbol).items():
        if not name.startswith("market."):
            assert res["reasons"] == ["bad_symbol"], name


def test_missing_folder_is_stale_unknown(tmp_path: Path) -> None:
    answers = _six(tmp_path / "nowhere")
    assert _values(answers) == {"unknown"} and all(r["stale"] for r in answers.values())


def test_every_exception_becomes_unknown(scout_dir: Path, cfg: ScoutConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    _world(scout_dir, cfg)

    def boom(*_a: object, **_k: object) -> Any:
        raise LookupError("boom")
    monkeypatch.setattr(gates, "fresh_catalyst_from", boom)
    monkeypatch.setattr(gates, "event_before_next_session_from", boom)
    assert api.fresh_catalyst(scout_dir, S, AS_OF)["reasons"] == ["error:LookupError"]
    assert api.event_before_next_session(scout_dir, S, AS_OF)["reasons"] == ["error:LookupError"]
    monkeypatch.setattr(api.R, "day_files", boom)
    for res in _six(scout_dir).values():
        assert res["value"] == "unknown"
    assert api.news_headline(scout_dir, S, AS_OF)["classes"] == []
    monkeypatch.setattr(api.Reader, "__init__", boom)
    assert api.halt_open(scout_dir, S, AS_OF)["reasons"] == ["error:LookupError"]
    assert set(api.jason_tags(scout_dir, S, None, AS_OF)) == set(JASON_TAGS)
    assert set(api.market_context(scout_dir, AS_OF)) == set(MARKET_FIELDS)


def test_unexpected_keyword_is_an_unknown_not_an_error(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    got = api.jason_tags(scout_dir, S, None, AS_OF, colour="red")
    assert {v["reasons"][0] for v in got.values()} == {"error:TypeError"}


# --------------------------------------------------------------------------- delegated answers


def test_halt_open_delegates_with_halt_facts_only(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    assert api.halt_open(scout_dir, S, AS_OF)["value"] == "false"
    halt = fact(S, published=AS_OF - timedelta(days=3), cls=None, kind="halt", source="nasdaq_halts", halt_code="T1")
    write_records(scout_dir, "facts", [halt, fact(S, published=AS_OF - timedelta(hours=1), cls="fda")])
    res = api.halt_open(scout_dir, S, AS_OF)
    assert res["value"] == "true" and res["evidence"] == [halt["fact_id"]]


@pytest.mark.parametrize("name, func", [("halts", "halt_open_from"), ("jason", "jason_tags_from"),
                                        ("macro", "market_context_from")])
def test_delegates_not_built_raising_or_malformed(scout_dir: Path, cfg: ScoutConfig, monkeypatch: pytest.MonkeyPatch,
                                                  name: str, func: str) -> None:
    _world(scout_dir, cfg)

    def call() -> list[dict[str, Any]]:
        r = Reader(scout_dir)
        if name == "halts":
            return [r.halt_open(S, AS_OF)]
        if name == "jason":
            got = r.jason_tags(S, 1.0, AS_OF)
            return [got[k] for k in JASON_TAGS]
        got = r.market_context(AS_OF)
        return [got[k] for k in MARKET_FIELDS]

    hide_module(monkeypatch, name)
    assert {tuple(r["reasons"]) for r in call()} == {(f"not_built:{name}",)}

    def boom(*_a: object, **_k: object) -> Any:
        raise KeyError("x")
    install_module(monkeypatch, name, **{func: boom})
    assert {tuple(r["reasons"]) for r in call()} == {("error:KeyError",)}
    install_module(monkeypatch, name, **{func: lambda *_a, **_k: None})
    assert {tuple(r["reasons"]) for r in call()} == {("error:bad_result",)}
    install_module(monkeypatch, name, **{func: lambda *_a, **_k: "true" if name == "halts" else {"x": 1}})
    assert all(r["value"] == "unknown" for r in call())


def test_halt_value_must_be_tri(scout_dir: Path, cfg: ScoutConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    _world(scout_dir, cfg)
    install_module(monkeypatch, "halts", halt_open_from=lambda *_a: tri("maybe"))
    assert api.halt_open(scout_dir, S, AS_OF)["reasons"] == ["error:bad_result"]


def test_jason_gets_the_latest_visible_snapshot_and_params(scout_dir: Path, cfg: ScoutConfig,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    _world(scout_dir, cfg)
    seen: list[tuple] = []

    def jason_tags_from(snapshot: Any, symbol: str, price: Any, as_of: datetime, near: Any, frame: Any,
                        market_symbols: Any = None) -> dict:
        seen.append((snapshot and snapshot["files"], symbol, price, near, frame, market_symbols))
        return {k: tri("false", extra=1) for k in JASON_TAGS}
    install_module(monkeypatch, "jason", jason_tags_from=jason_tags_from)
    snap = {"today_et": "2026-11-03", "setups": [], "warnings": []}
    write_records(scout_dir, "jason", [{"first_seen_at": iso(AS_OF - timedelta(days=1)), "files": ["a"], **snap},
                                       {"first_seen_at": iso(AS_OF - timedelta(hours=1)), "files": ["b"], **snap},
                                       {"first_seen_at": iso(AS_OF + timedelta(hours=1)), "files": ["c"], **snap}])
    got = Reader(scout_dir).jason_tags("acme", 3.5, AS_OF, frame_prices={"SPY": 600.0})
    assert seen[-1] == (["b"], "ACME", 3.5, cfg.jason_near_level_pct, {"SPY": 600.0}, ["SPY", "QQQ"])
    assert got["on_jason_list"]["extra"] == 1
    Reader(scout_dir).jason_tags(S, None, AS_OF, near_pct=0.2)
    assert seen[-1][3] == 0.2


def test_market_context_gets_visible_history_and_macro_facts(scout_dir: Path, cfg: ScoutConfig,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    _world(scout_dir, cfg)
    seen: list[tuple] = []

    def market_context_from(history: list, facts: list, params: dict, as_of: datetime, beat: Any = None) -> dict:
        seen.append(([h["source"] for h in history], [f["kind"] for f in facts], params["scout_stale_s"]))
        return {"macro_event_next_session": tri("false"), "inflation_regime": tri("hot"), "vix": tri("unknown"),
                "next_macro_event": tri("unknown"), "extra_field": 7}
    install_module(monkeypatch, "macro", market_context_from=market_context_from)
    write_records(scout_dir, "macro", [{"source": "nowflation", "first_seen_at": iso(AS_OF - timedelta(hours=2))},
                                       {"source": "bls", "first_seen_at": iso(AS_OF + timedelta(hours=2))}])
    write_records(scout_dir, "facts", [
        fact(None, published=AS_OF - timedelta(days=20), cls=None, kind="macro_event", event_at="2026-11-04T13:30:00Z"),
        fact(S, published=AS_OF - timedelta(hours=1), cls="fda")])
    got = Reader(scout_dir).market_context(AS_OF)
    assert seen[-1] == (["nowflation"], ["macro_event"], 120)
    assert got["inflation_regime"]["value"] == "hot" and got["extra_field"] == 7


# --------------------------------------------------------------------------- damage (rule 4)


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


@pytest.mark.parametrize("line, reason", [
    ("{not json\n", "unreadable_line:facts/" + DAY),
    ('{"format": 1, "kind": "wire"', "partial_line:facts/" + DAY),
    ('{"format": 2, "kind": "wire"}\n', "unknown_format:facts/" + DAY),
    ("[1, 2]\n", "unreadable_line:facts/" + DAY),
])
def test_damage_in_a_facts_file_turns_false_into_unknown(scout_dir: Path, cfg: ScoutConfig, line: str,
                                                         reason: str) -> None:
    _world(scout_dir, cfg)
    _append(scout_dir / "state" / "facts" / DAY, line)
    for gate in (api.fresh_catalyst, api.event_before_next_session, api.news_headline):
        res = gate(scout_dir, S, AS_OF)
        assert res["value"] == "unknown" and reason in res["reasons"], gate.__name__


def test_damage_does_not_hide_a_true(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    write_records(scout_dir, "facts", [fact(S, published=AS_OF - timedelta(minutes=3), cls="fda")])
    _append(scout_dir / "state" / "facts" / DAY, "{not json\n")
    assert api.fresh_catalyst(scout_dir, S, AS_OF)["value"] == "true"


def test_damage_in_due_and_health_files(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    _append(scout_dir / "state" / "edgar" / "due" / DAY, '{"format": 7}\n')
    assert api.event_before_next_session(scout_dir, S, AS_OF)["value"] == "unknown"
    assert api.fresh_catalyst(scout_dir, S, AS_OF)["value"] == "false"  # gate 6 does not read due records
    _append(scout_dir / "state" / "health" / DAY, "garbage\n")
    assert api.fresh_catalyst(scout_dir, S, AS_OF)["value"] == "unknown"


def test_params_damage(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    params = scout_dir / "state" / "params.jsonl"
    _append(params, json.dumps(params_record(cfg, AS_OF - timedelta(hours=1)) | {"format": 3}) + "\n")
    assert api.fresh_catalyst(scout_dir, S, AS_OF)["reasons"] == ["unknown_format:params"]
    assert api.fresh_catalyst(scout_dir, S, AS_OF - timedelta(hours=2))["value"] == "unknown"  # no beat then
    params.unlink()
    _append(params, json.dumps(params_record(cfg, AS_OF - timedelta(days=9))) + "\n{broken\n")
    assert api.fresh_catalyst(scout_dir, S, AS_OF)["reasons"] == ["unreadable_line:params"]
    _append(params, json.dumps(params_record(cfg, AS_OF - timedelta(days=8))) + "\n")
    assert api.fresh_catalyst(scout_dir, S, AS_OF)["value"] == "false"  # the damage is before the record in force


def test_unknown_format_beat_is_stale(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    _append(scout_dir / "state" / "beats" / DAY,
            json.dumps({"format": 2, "at": iso(AS_OF), "stopped": False, "sources": {}, "gap_open": {}}) + "\n")
    res = api.fresh_catalyst(scout_dir, S, AS_OF)
    assert res["stale"] is True and "unknown_format:beats" in res["reasons"]


# --------------------------------------------------------------------------- hold_allowed


def test_hold_allowed_only_for_true_false_fresh() -> None:
    assert hold_allowed(tri("true"), tri("false"))
    assert not hold_allowed(tri("false"), tri("false"))
    assert not hold_allowed(tri("true"), tri("unknown"))
    assert not hold_allowed(dict(tri("true"), stale=True), tri("false"))
    assert not hold_allowed(tri("true"), {"value": "false"})
    assert not hold_allowed(None, None)


_result = st.one_of(
    st.none(), st.text(max_size=5), st.integers(),
    st.fixed_dictionaries({"value": st.sampled_from(["true", "false", "unknown", "TRUE", "", 1]),
                           "stale": st.sampled_from([False, True, None, 0, "no"]),
                           "reasons": st.sampled_from([[], ["x"], None, "x"]),
                           "evidence": st.just([])}),
)


@settings(max_examples=300, deadline=None)
@given(_result, _result)
def test_property_only_true_false_fresh_allows_a_hold(g6: Any, g5: Any) -> None:
    allowed = hold_allowed(g6, g5)
    expected = (isinstance(g6, dict) and isinstance(g5, dict) and g6["value"] == "true" and g5["value"] == "false"
                and g6["stale"] is False and g5["stale"] is False and isinstance(g6["reasons"], list)
                and isinstance(g5["reasons"], list))
    assert allowed == expected


# --------------------------------------------------------------------------- fail closed (property tests)


@contextmanager
def _folder() -> Iterator[tuple[Path, ScoutConfig]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = (Path(tmp) / "Scout").resolve()
        root.mkdir()
        ScoutPaths(root).ensure()
        yield root, make_config(root)


def _beat_line(at: datetime, *, fmt: int = 1, stopped: bool = False) -> str:
    return json.dumps({"format": fmt, "at": iso(at), "stopped": stopped, "gap_open": {},
                       "sources": {"edgar_atom": "ok", "testwire": "ok", "prnewswire": "ok"}})


BEAT_DAMAGE = {
    "missing": "",
    "corrupt": "{\"format\": 1, \"at\": oops}\n",
    "partial": _beat_line(AS_OF),  # no newline: a writer mid-append
    "unknown_format": _beat_line(AS_OF, fmt=2) + "\n",
    "stale": _beat_line(AS_OF - timedelta(seconds=121)) + "\n",
    "stopped": _beat_line(AS_OF, stopped=True) + "\n",
    "future": _beat_line(AS_OF + timedelta(seconds=5)) + "\n",
}
PARAMS_DAMAGE = ["missing", "corrupt", "partial", "unknown_format", "future"]
GARBAGE = st.one_of(
    st.just("{broken\n"), st.just('{"format": 9, "ticker": "ACME"}\n'), st.just("null\n"),
    st.just('{"format": 1, "ticker": "ACME"'),
    st.binary(max_size=60).map(lambda b: b.decode("latin-1")),
    st.builds(lambda f: json.dumps(f) + "\n",
              st.builds(lambda cls: fact(S, published=AS_OF - timedelta(minutes=2), cls=cls), st.sampled_from(
                  ["fda", "earnings_date", "trial"]))),
)


def _params_text(cfg: ScoutConfig, mode: str) -> str:
    good = json.dumps(params_record(cfg, AS_OF - timedelta(days=2)))
    return {"missing": "", "corrupt": "{params\n", "partial": good,
            "unknown_format": json.dumps(params_record(cfg, AS_OF - timedelta(days=2)) | {"format": 2}) + "\n",
            "future": json.dumps(params_record(cfg, AS_OF + timedelta(seconds=1))) + "\n"}[mode]


@PROPERTY
@given(beat=st.sampled_from([None, *BEAT_DAMAGE]), params=st.sampled_from([None, *PARAMS_DAMAGE]),
       facts=st.lists(GARBAGE, max_size=4), due=st.lists(GARBAGE, max_size=3), health=st.lists(GARBAGE, max_size=3))
def test_property_damaged_beats_or_params_give_unknown(beat: str | None, params: str | None, facts: list[str],
                                                       due: list[str], health: list[str]) -> None:
    """Any mix of stale, missing, corrupt, partial-line or unknown-format beats or params files gives
    unknown everywhere (never true, never an exception), whatever the other files hold."""
    if beat is None and params is None:
        beat = "stale"
    with _folder() as (root, cfg):
        state = root / "state"
        if beat is None:
            write_beats(root, AS_OF - timedelta(seconds=60), AS_OF)
        elif BEAT_DAMAGE[beat]:
            _append(state / "beats" / DAY, BEAT_DAMAGE[beat])
        text = _params_text(cfg, params) if params is not None else json.dumps(
            params_record(cfg, AS_OF - timedelta(days=2))) + "\n"
        if text:
            _append(state / "params.jsonl", text)
        for folder, lines in (("facts", facts), ("edgar/due", due), ("health", health)):
            for line in lines:
                _append(state / folder / DAY, line)
        answers = _six(root)
        assert _values(answers) == {"unknown"}, answers
        assert not hold_allowed(answers["fresh_catalyst"], answers["event_before_next_session"])


@PROPERTY
@given(facts=st.lists(GARBAGE, max_size=3), due=st.lists(GARBAGE, max_size=3), health=st.lists(GARBAGE, max_size=3),
       keep_due=st.booleans())
def test_property_damaged_gate_files_never_give_false(facts: list[str], due: list[str], health: list[str],
                                                      keep_due: bool) -> None:
    """With Scout fresh, damage in a file a gate reads never yields that gate's `false`; damage alone
    never yields gate 6 `true` (a damaged line is never evidence)."""
    with _folder() as (root, cfg):
        _world(root, cfg, due=keep_due)
        state = root / "state"
        damaged: dict[str, bool] = {}
        for key, folder, lines in (("facts", "facts", facts), ("due", "edgar/due", due), ("health", "health", health)):
            for line in lines:
                _append(state / folder / DAY, line)
            damaged[key] = _file_damaged(state / folder / DAY)
        r = Reader(root)
        g6, g5 = r.fresh_catalyst(S, AS_OF), r.event_before_next_session(S, AS_OF)
        catalyst_lines = any(_is_good_fact(line, ("fda", "trial")) for line in facts)
        if not catalyst_lines:
            assert g6["value"] != "true"
        if damaged["facts"] or damaged["health"]:
            assert g6["value"] != "false"
        if damaged["facts"] or damaged["due"] or not keep_due:
            assert g5["value"] != "false"


def _file_damaged(path: Path) -> bool:
    """What the reader sees: a partial last line, or a complete line that is not a format-1 object."""
    if not path.exists():
        return False
    chunks = path.read_bytes().split(b"\n")
    if chunks[-1].strip():
        return True
    return any(raw.strip() and _is_bad(raw.strip().decode("utf-8", "replace")) for raw in chunks[:-1])


def _is_bad(line: str) -> bool:
    try:
        rec = json.loads(line)
    except ValueError:
        return True
    return not isinstance(rec, dict) or rec.get("format") != 1


def _is_good_fact(line: str, classes: tuple[str, ...]) -> bool:
    if not line.endswith("\n") or _is_bad(line):
        return False
    return json.loads(line).get("class") in classes


@settings(max_examples=60, deadline=None)
@given(st.dictionaries(st.sampled_from(["facts", "health", "beats", "edgar/due", "jason", "macro", "params"]),
                       st.binary(max_size=200), max_size=7))
def test_property_random_bytes_never_raise(files: dict[str, bytes]) -> None:
    with _folder() as (root, _cfg):
        for folder, body in files.items():
            if folder in ("jason", "macro", "params"):
                path = root / "state" / {"jason": "jason/snapshots.jsonl", "macro": "macro/history.jsonl",
                                         "params": "params.jsonl"}[folder]
            else:
                path = root / "state" / folder / DAY
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        answers = _six(root)
        assert _values(answers) == {"unknown"}


def test_jason_tags_note_damage_in_the_snapshot_file(scout_dir: Path, cfg: ScoutConfig,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the jason snapshot scan was thrown away, so a newer snapshot in a future format
    (or a torn newest line) silently fell back to the older snapshot and the tags said false."""
    _world(scout_dir, cfg)
    seen: list[Any] = []

    def jason_tags_from(snapshot: Any, *args: Any, **kw: Any) -> dict:
        seen.append(snapshot and snapshot["files"])
        return {k: tri("false") for k in JASON_TAGS}
    install_module(monkeypatch, "jason", jason_tags_from=jason_tags_from)
    snap = {"today_et": "2026-11-03", "setups": [], "warnings": []}
    write_records(scout_dir, "jason", [{"first_seen_at": iso(AS_OF - timedelta(days=1)), "files": ["old"], **snap},
                                       {"format": 2, "first_seen_at": iso(AS_OF - timedelta(hours=1)),
                                        "files": ["new"], **snap}])
    got = Reader(scout_dir).jason_tags(S, 1.0, AS_OF)
    assert seen[-1] == ["old"]
    assert all(got[k]["value"] == "unknown" for k in JASON_TAGS)
    assert all("unknown_format:jason/snapshots.jsonl" in got[k]["reasons"] for k in JASON_TAGS)


def test_market_context_passes_the_beat_for_rule_6(scout_dir: Path, cfg: ScoutConfig,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: Reader._market dropped the beat, so a macro source in error in the beat still
    answered true/false from a history line whose stale_after had not passed."""
    _world(scout_dir, cfg)
    seen: list[Any] = []

    def market_context_from(history: list, facts: list, params: dict, as_of: datetime, beat: Any = None) -> dict:
        seen.append(beat)
        return {k: tri("unknown") for k in ("macro_event_next_session", "inflation_regime", "vix",
                                            "next_macro_event")}
    install_module(monkeypatch, "macro", market_context_from=market_context_from)
    Reader(scout_dir).market_context(AS_OF)
    assert isinstance(seen[-1], dict) and "sources" in seen[-1]
