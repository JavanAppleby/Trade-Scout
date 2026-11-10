"""How often the Discord capture tasks find something new, and the schedule the rule suggests
(owner, 2026-11-10; tasks/README.md "How often the tasks run").

    .venv\\Scripts\\python.exe tools\\task_hit_rates.py [--days 7] [--target 0.9]

Run from Scout's folder (or pass --scout-dir). It reads Scout's own record of every capture file it
read, `state\\discord\\files.jsonl` (channel, capture time, new posts), and, for the time Jason's daily
card is posted, the `levels_card` lines of `inbox\\discord\\daily-watchlist\\` (only their `posted_at`).
It opens each file, reads it and closes it, writes nothing and changes no schedule: the owner (or a
Claude session the owner asks) applies a suggestion in the desktop app's Scheduled tasks.

A run is the set of capture files a task wrote with one capture time. A run "found something new"
when one of the task's every-run channels had a new post. The rule, per band of the US day:

    suggested interval = current interval x ln(1 - target) / ln(1 - hit rate)

the interval at which `target` (0.9: nine runs in ten) would find something new if posts arrive at
random, rounded UP to 10, 15, 20, 30, 60, 120 or 240 minutes (never below 10, the shortest cron a
local task takes; up, because nothing is lost by waiting: each run reads back to the last captured
post). It is stable: re-measured at the suggested interval it suggests the same one. A band with no
new post, or whose interval comes out longer than 4 hours, gets no run of its own: the next run
catches up. Posts that come at set times (Jason's card) are better served by one run just after
them, and one evening run keeps anything from waiting overnight: the report prints when the card
was posted.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scout_reader"))
from scout_reader._et import utc_to_et  # noqa: E402  (stdlib only)

#: Each task's every-run channels (tasks/*/SKILL.md): a fact of the task definitions, not a tunable.
TASKS: dict[str, tuple[str, ...]] = {
    "news": ("brianna-news", "stockmktnewz", "major-headlines", "earnings-whispers", "market-movers",
             "open-outcrier"),
    "jason": ("daily-watchlist", "weekly-trade-plan", "free-analysis"),
}
STEPS_MIN = (10, 15, 20, 30, 60, 120, 240)
BANDS = (("04:00-09:30", 4 * 60, 9 * 60 + 30), ("09:30-16:00", 9 * 60 + 30, 16 * 60),
         ("16:00-20:00", 16 * 60, 20 * 60))


@dataclass
class Run:
    at: datetime  # the capture time (UTC)
    new: dict[str, int] = field(default_factory=dict)  # channel -> new posts

    def found(self, channels: Iterable[str]) -> bool:
        return any(self.new.get(c, 0) > 0 for c in channels)


def _utc(text: object) -> datetime | None:
    if not isinstance(text, str) or not text.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def read_jsonl(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def runs_from(records: Iterable[Mapping], since: datetime) -> dict[str, list[Run]]:
    """Each task's runs since `since`, oldest first. A file booked twice (re-read) counts once, with
    the new posts of both bookings."""
    files: dict[str, tuple[str, str, int]] = {}
    for rec in records:
        name, channel, at = rec.get("file"), rec.get("channel"), rec.get("capture_at")
        new = rec.get("posts_new") if isinstance(rec.get("posts_new"), int) else 0
        if not isinstance(name, str) or not isinstance(channel, str) or _utc(at) is None:
            continue
        before = files.get(name)
        files[name] = (channel, at, new + (before[2] if before else 0))
    runs: dict[tuple[str, str], Run] = {}
    for channel, at, new in files.values():
        task = next((t for t, chans in TASKS.items() if channel in chans), None)
        when = _utc(at)
        if task is None or when is None or when < since:
            continue
        runs.setdefault((task, at), Run(when)).new[channel] = new
    out: dict[str, list[Run]] = {t: [] for t in TASKS}
    for (task, _at), run in runs.items():
        out[task].append(run)
    for task in out:
        out[task].sort(key=lambda r: r.at)
    return out


def band_of(at: datetime) -> str | None:
    et = utc_to_et(at)
    minute = et.hour * 60 + et.minute
    return next((name for name, a, b in BANDS if a <= minute < b), None)


def interval_min(runs: list[Run]) -> float | None:
    """The median time between consecutive runs on the same ET day, in minutes."""
    gaps = [(b.at - a.at).total_seconds() / 60 for a, b in zip(runs, runs[1:])
            if utc_to_et(a.at).date() == utc_to_et(b.at).date()]
    return statistics.median(gaps) if gaps else None


def suggest(current_min: float, hit_rate: float, target: float) -> int | None:
    """The rule (module doc); None when nothing new came, or the interval is over the largest step
    (no run of its own)."""
    if hit_rate <= 0:
        return None
    if hit_rate >= 1:
        return STEPS_MIN[0]
    raw = current_min * math.log(1 - target) / math.log(1 - hit_rate)
    return next((s for s in STEPS_MIN if s >= raw - 1e-9), None)


def card_times(scout_dir: Path, since: datetime) -> list[datetime]:
    """When Jason's daily cards were posted (ET), from the capture lines' `posted_at` only."""
    out = set()
    for path in sorted((scout_dir / "inbox" / "discord" / "daily-watchlist").glob("*.jsonl")):
        for rec in read_jsonl(path):
            at = _utc(rec.get("posted_at"))
            if rec.get("kind") == "levels_card" and at is not None and at >= since:
                out.add(utc_to_et(at))
    return sorted(out)


def report(scout_dir: Path, now: datetime, days: int, target: float) -> str:
    since = now - timedelta(days=days)
    runs = runs_from(read_jsonl(scout_dir / "state" / "discord" / "files.jsonl"), since)
    lines = [f"Capture tasks since {utc_to_et(since):%Y-%m-%d %H:%M} ET (last {days} days); target: {target:.0%} "
             "of runs find something new."]
    for task, channels in TASKS.items():
        task_runs = runs[task]
        if not task_runs:
            lines.append(f"\n{task}: no runs recorded.")
            continue
        hits = sum(r.found(channels) for r in task_runs)
        lines.append(f"\n{task}: {len(task_runs)} runs, {hits / len(task_runs):.0%} found something new.")
        for name, _a, _b in BANDS:
            band = [r for r in task_runs if band_of(r.at) == name]
            if not band:
                continue
            rate = sum(r.found(channels) for r in band) / len(band)
            every = interval_min(band) or interval_min(task_runs)
            tip = "-" if every is None else suggest(every, rate, target)
            shown = ("no run of its own (over 4 hours: the next run catches up)" if tip is None
                     else f"every {tip} min")
            lines.append(f"  {name} ET: {len(band)} runs about every {every or 0:.0f} min, {rate:.0%} found "
                         f"something new -> {shown}")
        for channel in channels:
            seen = [r for r in task_runs if channel in r.new]
            if seen:
                n = sum(1 for r in seen if r.new[channel] > 0)
                lines.append(f"    {channel}: new posts in {n} of {len(seen)} runs, "
                             f"{sum(r.new[channel] for r in seen)} posts")
    cards = card_times(scout_dir, since)
    if cards:
        latest = max(c.hour * 60 + c.minute for c in cards)
        lines.append(f"\nJason's daily card was posted between {min(c.strftime('%H:%M') for c in cards)} and "
                     f"{latest // 60:02d}:{latest % 60:02d} ET on {len(cards)} days: run the card task about "
                     "10 minutes after the latest.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scout-dir", type=Path, default=REPO)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--target", type=float, default=0.9)
    args = parser.parse_args(argv)
    if not 0 < args.target < 1 or args.days < 1:
        parser.error("--target must be between 0 and 1, --days at least 1")
    print(report(args.scout_dir, datetime.now(timezone.utc), args.days, args.target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
