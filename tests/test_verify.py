"""Verification (SCOUT_SPEC 11): the seeded weekly sample, the automatic gate-5 check, the report and
its pass criteria."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from helpers_fake_anthropic import fact, make_ctx
from helpers_fakes import write_records
from scout.calendar import is_trading_day
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.paths import ScoutPaths
from scout.verify import auto_check, cli_verify, gate5_checks, report, trading_days, week_bounds, weekly_sample

NOW = datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc)


def answer(symbol: str, as_of: str, *, g6: str = "unknown", g5: str = "unknown") -> dict[str, Any]:
    return {"symbol": symbol, "as_of": as_of, "written_at": as_of, "origin": "pilot", "fresh_catalyst": g6,
            "event_before_next_session": g5, "macro_event_next_session": "false", "news_headline": g6,
            "halt_open": "false", "reasons": {}, "evidence": {}, "stale": False}


def test_week_helpers() -> None:
    assert week_bounds("2026-W45") == (date(2026, 11, 2), date(2026, 11, 8))
    assert trading_days(date(2026, 11, 2), date(2026, 11, 8)) == 5


def test_weekly_sample_is_seeded_and_stored(scout_dir: Path, paths: ScoutPaths, tmp_path: Path) -> None:
    write_records(scout_dir, "answers", [answer(f"S{i:02d}", f"2026-11-0{2 + i % 4}T23:45:00Z") for i in range(15)])
    assert weekly_sample(paths, "2026-W44", None, now=NOW) == []  # nothing answered that week
    picked = weekly_sample(paths, "2026-W45", "seed-1", now=NOW)
    assert len(picked) == 10 and picked == sorted(picked)
    rows = [json.loads(x) for x in (paths.verify / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == [{"format": 1, "week": "2026-W45", "seed": "seed-1", "picked_at": "2026-11-06T22:00:00Z",
                     "symbols": picked}]
    assert weekly_sample(paths, "2026-W45", "other-seed", now=NOW) == picked  # stored pick wins
    # the same seed over the same answers picks the same symbols in another folder
    other = tmp_path / "Other"
    other.mkdir()
    op = ScoutPaths(other)
    op.ensure()
    write_records(other, "answers", [answer(f"S{i:02d}", f"2026-11-0{2 + i % 4}T23:45:00Z") for i in range(15)])
    assert weekly_sample(op, "2026-W45", "seed-1", now=NOW) == picked
    assert weekly_sample(op, "2026-W46", None, now=NOW) == []  # the future week


def test_gate5_auto_check(scout_dir: Path, paths: ScoutPaths) -> None:
    # Monday 19:45 ET answers; next session Tuesday, closing 16:00 ET = 21:00Z.
    write_records(scout_dir, "answers", [answer("MISS", "2026-11-03T00:45:00Z", g5="false"),
                                         answer("OKAY", "2026-11-03T00:45:00Z", g5="false"),
                                         answer("TRUE", "2026-11-03T00:45:00Z", g5="true")])
    write_records(scout_dir, "facts", [
        fact("MISS", "MISS Corp reports third quarter results", "2026-11-03T12:00:00Z", fid="m1",
             cls="earnings_result"),
        fact("OKAY", "OKAY files 10-Q", "2026-11-04T12:00:00Z", fid="o1", kind="filing", cls=None, form="10-Q"),
        fact("OKAY", "OKAY wins contract", "2026-11-03T12:00:00Z", fid="o2"),
        fact("TRUE", "TRUE reports results", "2026-11-03T12:00:00Z", fid="t1", cls="earnings_result"),
    ])
    pending = gate5_checks(paths, date(2026, 11, 2), date(2026, 11, 2), datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc))
    assert {r["status"] for r in pending} == {"pending"}
    rows = {r["symbol"]: r for r in gate5_checks(paths, date(2026, 11, 2), date(2026, 11, 2), NOW)}
    assert set(rows) == {"MISS", "OKAY"} and rows["OKAY"]["status"] == "ok" and rows["MISS"]["status"] == "miss"
    miss, = auto_check(paths, date(2026, 11, 2), NOW)
    assert miss.symbol == "MISS" and miss.fact_id == "m1"
    write_records(scout_dir, "facts", [fact("OKAY", "8-K", "2026-11-03T20:00:00Z", fid="o3", kind="filing", cls=None,
                                            form="8-K", items=("2.02",))])
    assert {m.symbol for m in auto_check(paths, date(2026, 11, 2), NOW)} == {"MISS", "OKAY"}


def _days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def test_report_pass_and_not_yet(scout_dir: Path, paths: ScoutPaths, cfg: ScoutConfig) -> None:
    days = _days(date(2026, 9, 1), 21)
    answers, checks = [], []
    for i, d in enumerate(days):
        for j in range(2):
            as_of = f"{d.isoformat()}T23:45:00Z"
            answers.append(answer(f"A{i}{j}", as_of, g6="true", g5="false"))
            checks.append({"format": 1, "week": "w", "symbol": f"A{i}{j}", "as_of": as_of, "gate": "fresh_catalyst",
                           "scout": "true", "owner": "agree", "note": "", "checked_at": as_of})
    write_records(scout_dir, "answers", answers)
    (paths.verify / "checks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in checks), encoding="utf-8")
    write_records(scout_dir, "health", [
        {"source": "prnewswire", "feed": "all", "kind": "wire", "enabled": True, "gate6": True,
         "start": "2026-09-01T12:00:00Z", "end": "2026-09-01T12:00:01Z", "result": r, "http_status": 200, "items": 20,
         "new_items": 1, "overlap": ov, "spacing_s": 60, "gap_before": gap, "error": None}
        for r, ov, gap in (("ok", True, False), ("timeout", None, True), ("ok", False, True))])
    text = report(paths, days[0], days[-1], cfg=cfg, now=NOW)
    assert "**Status: PASS.**" in text and "| fresh_catalyst | 42 | 0 | 0 | 0.0% |" in text
    assert "Checked 42 (pending 0); misses 0." in text and "| prnewswire | 3 | 1 | 1 | 2 |" in text
    assert "| fresh_catalyst | 42 | 42 | 0 | 0 | 0 |" in text
    # one disagreement in four -> agreement below 90%
    bad = [dict(c, owner="disagree") for c in checks[:5]]
    (paths.verify / "checks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in bad + checks[5:]),
                                               encoding="utf-8")
    text = report(paths, days[0], days[-1], cfg=cfg, now=NOW)
    assert "**Status: NOT YET.**" in text and "[ ] gate-6 true agreement at least 90%: 88.1%" in text
    short = report(paths, days[0], days[5], cfg=cfg, now=NOW)
    assert "[ ] at least 20 trading days: 6" in short


def test_cli_verify(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, scout_dir: Path) -> None:
    ctx = make_ctx(cfg, paths, clock)
    assert cli_verify(ctx, argparse.Namespace(date_from="x", date_to=None)) == 2
    assert cli_verify(ctx, argparse.Namespace(date_from="2026-11-05", date_to="2026-11-01")) == 2
    write_records(scout_dir, "answers", [answer("ABC", "2026-10-28T23:45:00Z")])
    assert cli_verify(ctx, argparse.Namespace(date_from=None, date_to=None)) == 0
    text = (paths.reports / "scout-verify-2026-11-02.md").read_text(encoding="utf-8")
    assert "2026-10-28 to 2026-11-02" in text and "NOT YET" in text
