"""tools/task_hit_rates.py: the capture tasks' hit rates and the schedule rule (owner, 2026-11-10;
tasks/README.md "How often the tasks run"). Synthetic bookkeeping only; nothing is written."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("task_hit_rates", REPO / "tools" / "task_hit_rates.py")
thr = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["task_hit_rates"] = thr  # its dataclass looks its module up while it is built
_spec.loader.exec_module(thr)

MON_0400_ET = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def _z(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _book(rows: list[dict], channel: str, at: datetime, new: int) -> None:
    stamp = at.strftime("%Y%m%dT%H%M%SZ")
    rows.append({"format": 1, "file": f"{channel}/{stamp}.jsonl", "channel": channel, "capture_at": _z(at),
                 "posts_new": new, "lines": new})


def _scout(tmp_path: Path) -> tuple[Path, list[dict]]:
    """Two weekdays: the news task every 10 min 04:00-20:00 ET, new headlines in 4 runs of 5; the
    Jason task every 15 min, new posts only in the run after the 09:10 ET card."""
    rows: list[dict] = []
    for day in range(2):
        start = MON_0400_ET + timedelta(days=day)
        for i in range(96):
            at = start + timedelta(minutes=10 * i)
            for channel in thr.TASKS["news"]:
                _book(rows, channel, at, 2 if channel == "major-headlines" and i % 5 else 0)
        for i in range(64):
            at = start + timedelta(minutes=15 * i, seconds=30)
            for channel in thr.TASKS["jason"]:
                _book(rows, channel, at, 1 if channel == "daily-watchlist" and i == 21 else 0)
    rows.append({**rows[0], "posts_new": 3})  # the same file booked again (re-read): counted once, 3 more posts
    scout = tmp_path / "Scout"
    (scout / "state" / "discord").mkdir(parents=True)
    with (scout / "state" / "discord" / "files.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        fh.write("not json\n")
    cards = scout / "inbox" / "discord" / "daily-watchlist"
    cards.mkdir(parents=True)
    card = {"kind": "levels_card", "posted_at": _z(MON_0400_ET + timedelta(hours=5, minutes=10)), "text": "x"}
    (cards / "20261102T141530Z.jsonl").write_text(json.dumps(card) + "\n", encoding="utf-8")
    return scout, rows


def test_the_rule_rounds_up_and_is_stable() -> None:
    assert thr.suggest(10, 0.80, 0.9) == 15  # 10 x ln(0.1)/ln(0.2) = 14.3 min
    assert thr.suggest(10, 0.84, 0.9) == 15  # 12.6 min: up, nothing is lost by waiting
    assert thr.suggest(10, 0.71, 0.9) == 20  # 18.6 min
    assert thr.suggest(10, 0.97, 0.9) == 10  # never below 10
    assert thr.suggest(10, 1.0, 0.9) == 10
    assert thr.suggest(15, 0.05, 0.9) is None  # 11 hours: no run of its own
    assert thr.suggest(10, 0.0, 0.9) is None  # nothing new at all
    at_15 = 1 - (1 - 0.80) ** 1.5  # the hit rate the same posts give at 15 min
    assert thr.suggest(15, at_15, 0.9) == 15  # re-measured at the suggestion: the same suggestion


def test_runs_are_grouped_by_capture_time_and_task(tmp_path: Path) -> None:
    _scout_dir, rows = _scout(tmp_path)
    runs = thr.runs_from(rows, MON_0400_ET)
    assert len(runs["news"]) == 192 and len(runs["jason"]) == 128
    first = runs["news"][0]
    assert first.new["brianna-news"] == 3 and set(first.new) == set(thr.TASKS["news"])  # re-read file: summed once
    assert thr.runs_from(rows, MON_0400_ET + timedelta(days=1))["news"][0].at == MON_0400_ET + timedelta(days=1)
    assert thr.interval_min(runs["news"]) == 10


def test_the_report(tmp_path: Path) -> None:
    scout, _rows = _scout(tmp_path)
    before = sorted((p, p.stat().st_mtime_ns) for p in scout.rglob("*"))
    text = thr.report(scout, MON_0400_ET + timedelta(days=2), 7, 0.9)
    assert "news: 192 runs, 80% found something new." in text
    assert "04:00-09:30 ET: 66 runs about every 10 min, 80% found something new -> every 15 min" in text
    assert "jason: 128 runs, 2% found something new." in text
    assert "no run of its own" in text
    assert "Jason's daily card was posted between 09:10 and 09:10 ET on 1 days" in text
    assert sorted((p, p.stat().st_mtime_ns) for p in scout.rglob("*")) == before  # it writes nothing
    assert thr.main(["--scout-dir", str(scout), "--days", "1"]) == 0
