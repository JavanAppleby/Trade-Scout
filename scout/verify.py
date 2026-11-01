"""Verifying the sources before any hold (SCOUT_SPEC 11, DESIGN 2.6 and 5.10).

- `weekly_sample`: 10 answered symbols a week, picked with a stored seed (`state/verify/
  samples.jsonl`), so the pick can be repeated; the owner hand-checks them (`scout.verify_check`).
- `gate5_checks` / `auto_check`: every gate-5 `false` answer against the filings and wires first
  seen after its as-of and before the next session's close; an earnings report there is a miss.
- `report`: `reports/scout-verify-<date>.md` with per-answer counts, unknown share, hand-check
  agreement, false positives, misses, per-source coverage gaps and the SS 11 pass criteria:
  at least `verify_days` trading days, at least 30 checked gate-5 `false` answers with zero misses,
  and gate-6 `true` agreeing with the hand check at least 90% of the time over at least 30 checks.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from scout_reader import records as R

from scout import FORMAT
from scout.brief_sections import iso_week, md_escape
from scout.calendar import core_close, is_trading_day, next_open_after
from scout.clock import et_date, et_datetime, fmt_et_bne, iso, parse_iso
from scout.config import ScoutConfig
from scout.paths import ScoutPaths
from scout.store import append_jsonl, atomic_write_text, read_jsonl

GATES = ("fresh_catalyst", "event_before_next_session", "macro_event_next_session", "news_headline", "halt_open")
PASS_MIN_GATE5_FALSE = 30
PASS_MIN_GATE6_CHECKS = 30
PASS_GATE6_AGREEMENT = 0.90
EARNINGS_FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F")

__all__ = [
    "GATES", "Miss", "answers_between", "auto_check", "cli_verify", "gate5_checks", "iso_week", "report",
    "samples", "trading_days", "week_bounds", "weekly_sample", "write_report",
]


@dataclass(frozen=True)
class Miss:
    symbol: str
    as_of: str
    fact_id: str
    title: str | None
    first_seen_at: str
    kind: str


def week_bounds(week: str) -> tuple[date, date]:
    """Monday and Sunday of an ISO week "YYYY-Www"."""
    y, w = week.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    return monday, monday + timedelta(days=6)


def trading_days(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        n += is_trading_day(d)
        d += timedelta(days=1)
    return n


def answers_between(paths: ScoutPaths, start: date, end: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = start
    while d <= end:
        out.extend(r for r in read_jsonl(paths.day_file(paths.answers, d)) if R.known(r) and r.get("symbol"))
        d += timedelta(days=1)
    return out


def samples(paths: ScoutPaths) -> list[dict[str, Any]]:
    return [r for r in read_jsonl(paths.verify / "samples.jsonl") if R.known(r)]


def weekly_sample(paths: ScoutPaths, week: str, seed: str | None = None, *, now: datetime,
                  size: int = 10) -> list[str]:
    """This week's sample: the stored one, else `size` symbols answered during the week up to
    `now`, picked with `seed` (new random when None) and stored with it. [] when nothing was
    answered yet (nothing is stored then)."""
    for rec in samples(paths):
        if rec.get("week") == week:
            return [str(s) for s in rec.get("symbols") or []]
    monday, sunday = week_bounds(week)
    last = min(sunday, et_date(now))
    if last < monday:
        return []
    symbols = sorted({str(r["symbol"]).upper() for r in answers_between(paths, monday, last)
                      if (parse_iso(r.get("written_at")) or now) <= now})
    if not symbols:
        return []
    seed = seed or secrets.token_hex(8)
    rng = random.Random(int(hashlib.sha256(f"{seed}|{week}".encode()).hexdigest(), 16))
    picked = sorted(rng.sample(symbols, min(size, len(symbols))))
    append_jsonl(paths, paths.verify / "samples.jsonl",
                 {"format": FORMAT, "week": week, "seed": seed, "picked_at": iso(now), "symbols": picked})
    return picked


# --------------------------------------------------------------------------- the automatic gate-5 check


def _facts_between(paths: ScoutPaths, start: datetime, end: datetime) -> list[dict[str, Any]]:
    out = []
    for path in R.day_files(paths.facts, start, end):
        for rec in read_jsonl(path):
            seen = parse_iso(rec.get("first_seen_at"))
            if R.known(rec) and seen is not None and start < seen <= end:
                out.append(rec)
    return out


def _is_earnings(fact: dict[str, Any]) -> bool:
    if fact.get("class") == "earnings_result":
        return True
    form = str(fact.get("form") or "").upper()
    return fact.get("kind") == "filing" and (form in EARNINGS_FORMS or "2.02" in (fact.get("items") or []))


def gate5_checks(paths: ScoutPaths, start: date, end: date, now: datetime) -> list[dict[str, Any]]:
    """Every gate-5 `false` answer in [start, end]: status `miss`, `ok` or `pending` (the next
    session has not closed by `now`), with the evidence of a miss."""
    out = []
    for ans in answers_between(paths, start, end):
        if ans.get("event_before_next_session") != "false":
            continue
        as_of = parse_iso(ans.get("as_of"))
        if as_of is None:
            continue
        nxt = next_open_after(as_of)
        close = core_close(et_date(nxt)) if nxt else None
        row: dict[str, Any] = {"symbol": str(ans["symbol"]).upper(), "as_of": ans.get("as_of"), "status": "pending",
                               "misses": []}
        if close is None or close > now:
            out.append(row)
            continue
        for f in _facts_between(paths, as_of, close):
            if str(f.get("ticker") or "").upper() == row["symbol"] and _is_earnings(f):
                row["misses"].append(Miss(row["symbol"], str(ans.get("as_of")), str(f.get("fact_id")), f.get("title"),
                                          str(f.get("first_seen_at")), str(f.get("kind"))))
        row["status"] = "miss" if row["misses"] else "ok"
        out.append(row)
    return out


def auto_check(paths: ScoutPaths, day: date, now: datetime | None = None) -> list[Miss]:
    """The misses among `day`'s gate-5 `false` answers."""
    at = now or et_datetime(day + timedelta(days=7), time(0, 0))
    return [m for row in gate5_checks(paths, day, day, at) for m in row["misses"]]


# --------------------------------------------------------------------------- the report


def _checks(paths: ScoutPaths, start: date, end: date) -> list[dict[str, Any]]:
    out = []
    for rec in read_jsonl(paths.verify / "checks.jsonl"):
        at = parse_iso(rec.get("as_of")) or parse_iso(rec.get("checked_at"))
        if R.known(rec) and at is not None and start <= et_date(at) <= end:
            out.append(rec)
    return out


def _health(paths: ScoutPaths, start: date, end: date) -> dict[str, Counter]:
    per: dict[str, Counter] = {}
    d = start
    while d <= end:
        for rec in read_jsonl(paths.day_file(paths.health, d)):
            if not R.known(rec):
                continue
            c = per.setdefault(str(rec.get("source")), Counter())
            c["polls"] += 1
            if rec.get("result") not in ("ok", "not_modified"):
                c["failed"] += 1
            if rec.get("overlap") is False:
                c["no_overlap"] += 1
            if rec.get("gap_before"):
                c["gaps"] += 1
        d += timedelta(days=1)
    return per


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def report(paths: ScoutPaths, start: date, end: date, *, cfg: ScoutConfig, now: datetime) -> str:
    answers = answers_between(paths, start, end)
    days = trading_days(start, end)
    lines = [f"# Scout verification report: {start} to {end}", "",
             f"Written {fmt_et_bne(now)}. Record only: nothing here allows a hold; the owner decides (SS 11).", "",
             f"Trading days in the period: {days} (needed: {cfg.verify_days}). Answers: {len(answers)}.", "",
             "## Answers by gate", "", "| Answer | true | false | unknown | unknown share |", "|---|---|---|---|---|"]
    for gate in GATES:
        c = Counter(str(a.get(gate)) for a in answers)
        total = sum(c.values())
        lines.append(f"| {gate} | {c['true']} | {c['false']} | {c['unknown']} | {_pct(c['unknown'], total)} |")

    rows = gate5_checks(paths, start, end, now)
    checked = [r for r in rows if r["status"] != "pending"]
    misses = [m for r in rows for m in r["misses"]]
    lines += ["", "## Gate 5: automatic check of every `false`", "",
              f"Checked {len(checked)} (pending {len(rows) - len(checked)}); misses {len(misses)}."]
    for m in misses:
        lines.append(f"- MISS {md_escape(m.symbol, 12)} answered {m.as_of}: {md_escape(m.kind, 10)} "
                     f"{md_escape(m.title, 140)} first seen {m.first_seen_at}")

    checks = _checks(paths, start, end)
    lines += ["", "## Owner's hand checks", "", "| Answer | checked | agree | disagree | false positives | misses |",
              "|---|---|---|---|---|---|"]
    for gate in GATES:
        g = [c for c in checks if c.get("gate") == gate]
        if not g:
            continue
        agree = sum(c.get("owner") == "agree" for c in g)
        fp = sum(c.get("owner") == "disagree" and c.get("scout") == "true" for c in g)
        miss = sum(c.get("owner") == "disagree" and c.get("scout") == "false" for c in g)
        lines.append(f"| {gate} | {len(g)} | {agree} | {len(g) - agree} | {fp} | {miss} |")
    g6 = [c for c in checks if c.get("gate") == "fresh_catalyst" and c.get("scout") == "true"]
    g6_agree = sum(c.get("owner") == "agree" for c in g6)

    lines += ["", "## Coverage by source", "", "| Source | polls | failed | no overlap | gaps |", "|---|---|---|---|---|"]
    for name, c in sorted(_health(paths, start, end).items()):
        lines.append(f"| {md_escape(name, 24)} | {c['polls']} | {c['failed']} | {c['no_overlap']} | {c['gaps']} |")

    crit = [
        (f"at least {cfg.verify_days} trading days", days >= cfg.verify_days, f"{days}"),
        (f"at least {PASS_MIN_GATE5_FALSE} checked gate-5 false answers", len(checked) >= PASS_MIN_GATE5_FALSE,
         f"{len(checked)}"),
        ("zero gate-5 misses", not misses, f"{len(misses)}"),
        (f"at least {PASS_MIN_GATE6_CHECKS} hand checks of gate-6 true", len(g6) >= PASS_MIN_GATE6_CHECKS, f"{len(g6)}"),
        (f"gate-6 true agreement at least {PASS_GATE6_AGREEMENT:.0%}",
         bool(g6) and g6_agree / len(g6) >= PASS_GATE6_AGREEMENT, _pct(g6_agree, len(g6))),
    ]
    passed = all(ok for _, ok, _ in crit)
    lines += ["", "## Pass criteria (SS 11)", ""]
    lines += [f"- [{'x' if ok else ' '}] {name}: {value}" for name, ok, value in crit]
    lines += ["", f"**Status: {'PASS' if passed else 'NOT YET'}.** "
              + ("The owner may set pilot_hold_enabled = true on paper first." if passed
                 else "Holds stay off until every criterion passes."), ""]
    return "\n".join(lines)


def write_report(paths: ScoutPaths, start: date, end: date, *, cfg: ScoutConfig, now: datetime) -> Path:
    target = paths.reports / f"scout-verify-{end.isoformat()}.md"
    atomic_write_text(paths, target, report(paths, start, end, cfg=cfg, now=now))
    return target


def first_answer_day(paths: ScoutPaths) -> date | None:
    days = []
    try:
        for p in paths.answers.glob("*.jsonl"):
            try:
                days.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
    except OSError:
        return None
    return min(days) if days else None


def cli_verify(ctx: Any, args: argparse.Namespace) -> int:
    now = ctx.clock.now()
    try:
        end = date.fromisoformat(args.date_to) if getattr(args, "date_to", None) else et_date(now)
        start = (date.fromisoformat(args.date_from) if getattr(args, "date_from", None)
                 else first_answer_day(ctx.paths) or end)
    except ValueError:
        print("Scout: dates must be YYYY-MM-DD")
        return 2
    if start > end:
        print("Scout: --from is after --to")
        return 2
    path = write_report(ctx.paths, start, end, cfg=ctx.cfg, now=now)
    print(f"Verification report written: {path}")
    return 0
