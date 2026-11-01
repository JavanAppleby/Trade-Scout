"""`python -m scout check`: the owner's weekly hand checks (SCOUT_SPEC 11)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from helpers_fake_anthropic import make_ctx
from helpers_fakes import write_records
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.paths import ScoutPaths
from scout.verify_check import checked, cli_check, record_check


def _answers(scout_dir: Path) -> None:
    rows = []
    for sym, g6, g5 in (("ABC", "true", "false"), ("XYZ", "false", "unknown")):
        rows.append({"symbol": sym, "as_of": "2026-11-02T00:45:00Z", "written_at": "2026-11-02T00:45:00Z",
                     "origin": "pilot", "fresh_catalyst": "unknown", "event_before_next_session": "unknown",
                     "macro_event_next_session": "false", "news_headline": "false", "halt_open": "false",
                     "reasons": {}, "evidence": {}, "stale": False})  # Sunday ET: the prior week
        rows.append({"symbol": sym, "as_of": "2026-11-03T00:45:00Z", "written_at": "2026-11-03T00:45:00Z",
                     "origin": "pilot", "fresh_catalyst": g6, "event_before_next_session": g5,
                     "macro_event_next_session": "false", "news_headline": g6, "halt_open": "false",
                     "reasons": {}, "evidence": {"fresh_catalyst": ["fact:abc"]}, "stale": False})
    write_records(scout_dir, "answers", rows)


def _script(*replies: str) -> tuple[list[str], object]:
    asked: list[str] = []
    it: Iterator[str] = iter(replies)

    def ask(prompt: str) -> str:
        asked.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return asked, ask


@pytest.fixture
def ctx(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock):  # noqa: ANN201
    clock.set(clock.now().replace(day=4))
    return make_ctx(cfg, paths, clock)


def test_check_flow(ctx, scout_dir: Path, paths: ScoutPaths) -> None:  # noqa: ANN001
    _answers(scout_dir)
    out: list[str] = []
    asked, ask = _script("a", "matches the PR", "d", "10-Q due Thursday", "", "agree", "")
    assert cli_check(ctx, argparse.Namespace(week=None), ask=ask, say=out.append) == 0
    assert "Verification sample for 2026-W45: ABC, XYZ" in out[0]
    assert "ABC fresh_catalyst = true as of 2026-11-03T00:45:00Z (evidence: fact:abc)" in asked[0]
    rows = [json.loads(x) for x in (paths.verify / "checks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["symbol"], r["gate"], r["scout"], r["owner"]) for r in rows] == [
        ("ABC", "fresh_catalyst", "true", "agree"), ("ABC", "event_before_next_session", "false", "disagree"),
        ("XYZ", "event_before_next_session", "unknown", "agree")]
    assert rows[1]["note"] == "10-Q due Thursday" and rows[0]["week"] == "2026-W45" and rows[0]["format"] == 1
    assert checked(paths, "2026-W45") == {("ABC", "fresh_catalyst"), ("ABC", "event_before_next_session"),
                                          ("XYZ", "event_before_next_session")}
    asked2, ask2 = _script("a", "fine")
    cli_check(ctx, argparse.Namespace(week="2026-W45"), ask=ask2, say=out.append)
    assert len(asked2) == 2 and asked2[0].startswith("XYZ fresh_catalyst")  # checked ones are skipped
    assert sum(1 for _ in (paths.verify / "checks.jsonl").open(encoding="utf-8")) == 4


def test_eof_and_bad_input(ctx, scout_dir: Path) -> None:  # noqa: ANN001
    out: list[str] = []
    assert cli_check(ctx, argparse.Namespace(week="2026-45"), ask=lambda p: "", say=out.append) == 2
    assert cli_check(ctx, argparse.Namespace(week=None), ask=lambda p: "", say=out.append) == 0
    assert "nothing to check" in out[-1]
    _answers(scout_dir)
    _, ask = _script()
    assert cli_check(ctx, argparse.Namespace(week=None), ask=ask, say=out.append) == 0
    assert out[-1].startswith("Stopped; 0 checks saved")


def test_record_check_validates(paths: ScoutPaths, clock: ManualClock) -> None:
    with pytest.raises(ValueError):
        record_check(paths, week="2026-W45", symbol="abc", as_of=None, gate="fresh_catalyst", scout="true",
                     owner="maybe", note="", now=clock.now())
    rec = record_check(paths, week="2026-W45", symbol="abc", as_of=None, gate="fresh_catalyst", scout="true",
                       owner="agree", note="line\nbreak " + "x" * 600, now=clock.now())
    assert rec["symbol"] == "ABC" and "\n" not in rec["note"] and len(rec["note"]) == 500
