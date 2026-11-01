"""No look-ahead and replay (SCOUT_SPEC 2 rule 9, 14; DESIGN 3.2 rule 1): a fact first seen after t
is never returned for t, and a stored day replays to identical answers."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from scout_reader.api import Reader

from scout.config import ScoutConfig
from scout.paths import ScoutPaths

from conftest import et_at, make_config
from helpers_answers import due_record, fact, good_world, install_all_stubs, write_beats, write_health, write_params
from helpers_fakes import write_records

AS_OF = et_at("2026-11-03", "19:45")
S = "ACME"
CLASSES = ["fda", "trial", "contract", "earnings_result", "offering", "other", "earnings_date", None]
TITLES = ["Acme to Report Results on November 4, 2026", "Acme to Report Results", "Acme wins award"]


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    install_all_stubs(monkeypatch)


@contextmanager
def _folder() -> Iterator[tuple[Path, ScoutConfig]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = (Path(tmp) / "Scout").resolve()
        root.mkdir()
        ScoutPaths(root).ensure()
        yield root, make_config(root)


def _answers(root: Path, as_of: datetime) -> dict:
    reader = Reader(root)
    return {"g6": reader.fresh_catalyst(S, as_of), "g5": reader.event_before_next_session(S, as_of),
            "news": reader.news_headline(S, as_of), "halt": reader.halt_open(S, as_of),
            "market": reader.market_context(as_of), "jason": reader.jason_tags(S, None, as_of)}


_fact = st.tuples(
    st.integers(min_value=-6 * 3600, max_value=6 * 3600),  # first seen, seconds from as_of
    st.integers(min_value=-48 * 3600, max_value=3600),  # published, seconds before first seen (negative: later)
    st.sampled_from(CLASSES), st.sampled_from(["wire", "filing", "halt"]), st.sampled_from(TITLES),
)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(_fact, max_size=6))
def test_a_fact_first_seen_after_t_is_never_returned_for_t(rows: list) -> None:
    """Answers with every fact equal answers with only the facts visible at as_of."""
    facts = []
    for i, (seen_s, lag_s, cls, kind, title) in enumerate(rows):
        seen = AS_OF + timedelta(seconds=seen_s)
        published = seen - timedelta(seconds=lag_s)
        facts.append(fact(S, published=published, seen=seen, cls=cls, kind=kind, title=title, guid=f"g{i}",
                          source="nasdaq_halts" if kind == "halt" else "prnewswire"))
    visible = [f for f in facts if f["first_seen_at"] <= _iso(AS_OF) and f["published_at"] <= _iso(AS_OF)]
    with _folder() as (full, cfg), _folder() as (seen_only, cfg2):
        for root, c, rows_ in ((full, cfg, facts), (seen_only, cfg2, visible)):
            good_world(root, c, AS_OF)
            write_records(root, "due", [due_record(S, AS_OF - timedelta(minutes=1), due_start="2027-01-10",
                                                   due_end="2027-04-05")])
            if rows_:
                write_records(root, "facts", rows_)
        got, want = _answers(full, AS_OF), _answers(seen_only, AS_OF)
        assert got == want
        hidden = {f["fact_id"] for f in facts} - {f["fact_id"] for f in visible}
        for res in (got["g6"], got["g5"], got["news"], got["halt"]):
            assert not hidden & set(res["evidence"])


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_replaying_a_stored_day_gives_identical_answers(scout_dir: Path, cfg: ScoutConfig) -> None:
    times = [et_at("2026-11-03", hhmm) for hhmm in ("16:30", "18:00", "19:30", "19:45")]
    good_world(scout_dir, cfg, times[-1], since=et_at("2026-11-02", "15:00"))
    for t in times:
        write_beats(scout_dir, t - timedelta(seconds=60), t)
        write_records(scout_dir, "due", [due_record(S, t - timedelta(seconds=30), due_start="2027-01-10",
                                                    due_end="2027-04-05")])
    write_records(scout_dir, "facts", [
        fact(S, published=et_at("2026-11-03", "17:00"), cls="contract"),
        fact(S, published=et_at("2026-11-03", "18:30"), cls="earnings_date",
             title="Acme to Report Results on November 4, 2026"),
    ])
    first = {t: _answers(scout_dir, t) for t in times}
    assert first[times[0]]["g6"]["value"] == "false" and first[times[1]]["g6"]["value"] == "true"
    assert first[times[1]]["g5"]["value"] == "false" and first[times[2]]["g5"]["value"] == "true"
    # Scout keeps running: later facts, beats, polls, due records and new params are appended
    later = et_at("2026-11-03", "19:46")
    write_records(scout_dir, "facts", [fact(S, published=later, cls="fda"),
                                       fact(S, published=times[0] - timedelta(minutes=5), seen=later, cls="trial")])
    write_beats(scout_dir, later, later + timedelta(minutes=5), stopped=True)
    write_health(scout_dir, later, later + timedelta(minutes=5), result="timeout")
    write_records(scout_dir, "due", [due_record(S, later, nt_recent=True)])
    write_params(scout_dir, cfg, later, hold_catalyst_classes=["other"], scout_stale_s=30)
    assert {t: _answers(scout_dir, t) for t in times} == first
