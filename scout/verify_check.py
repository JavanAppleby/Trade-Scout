"""`python -m scout check`: the owner's weekly hand checks (SCOUT_SPEC 11, DESIGN 2.6 and 5.10).

Lists the week's sample (picking it, with a stored seed, if none exists yet) and, for each symbol's
latest gate-6 and gate-5 answer that week, asks the owner to mark agree or disagree with a note
(EDGAR, the issuer's news page, a web search). Each mark is appended to `state/verify/checks.jsonl`;
a (week, symbol, gate) already checked is skipped. Nothing else is written.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from typing import Any

from scout_reader import records as R

from scout import FORMAT
from scout.brief_sections import iso_week
from scout.clock import et_date, iso, parse_iso
from scout.paths import ScoutPaths
from scout.store import append_jsonl, read_jsonl
from scout.verify import answers_between, week_bounds, weekly_sample

CHECK_GATES = ("fresh_catalyst", "event_before_next_session")
NOTE_MAX = 500

__all__ = ["CHECK_GATES", "checked", "cli_check", "latest_answers", "record_check"]


def checked(paths: ScoutPaths, week: str) -> set[tuple[str, str]]:
    return {(str(r.get("symbol")), str(r.get("gate"))) for r in read_jsonl(paths.verify / "checks.jsonl")
            if R.known(r) and r.get("week") == week}


def latest_answers(paths: ScoutPaths, week: str, now: datetime) -> dict[str, dict[str, Any]]:
    monday, sunday = week_bounds(week)
    out: dict[str, dict[str, Any]] = {}
    for rec in answers_between(paths, monday, min(sunday, et_date(now))):
        sym = str(rec["symbol"]).upper()
        if sym not in out or str(rec.get("as_of")) >= str(out[sym].get("as_of")):
            out[sym] = rec
    return out


def record_check(paths: ScoutPaths, *, week: str, symbol: str, as_of: str | None, gate: str, scout: str,
                 owner: str, note: str, now: datetime) -> dict[str, Any]:
    if owner not in ("agree", "disagree"):
        raise ValueError("owner must be 'agree' or 'disagree'")
    rec = {"format": FORMAT, "week": week, "symbol": symbol.upper(), "as_of": as_of, "gate": gate, "scout": scout,
           "owner": owner, "note": " ".join(str(note).split())[:NOTE_MAX], "checked_at": iso(now)}
    append_jsonl(paths, paths.verify / "checks.jsonl", rec)
    return rec


def cli_check(ctx: Any, args: argparse.Namespace, *, ask: Callable[[str], str] = input,
              say: Callable[[str], Any] = print) -> int:
    now = ctx.clock.now()
    week = getattr(args, "week", None) or iso_week(et_date(now))
    try:
        week_bounds(week)
    except (ValueError, IndexError):
        say("Scout: --week must look like 2026-W45")
        return 2
    symbols = weekly_sample(ctx.paths, week, None, now=now, size=ctx.cfg.verify_sample_size)
    if not symbols:
        say(f"No answers yet in {week}, so there is nothing to check.")
        return 0
    answers = latest_answers(ctx.paths, week, now)
    done = checked(ctx.paths, week)
    say(f"Verification sample for {week}: {', '.join(symbols)}")
    say("For each answer type 'a' (agree), 'd' (disagree) or Enter to skip; then a short note.")
    written = 0
    for symbol in symbols:
        ans = answers.get(symbol)
        if ans is None:
            say(f"{symbol}: no answer found this week; skipped.")
            continue
        at = parse_iso(ans.get("as_of"))
        for gate in CHECK_GATES:
            if (symbol, gate) in done:
                continue
            value = str(ans.get(gate))
            evidence = ", ".join((ans.get("evidence") or {}).get(gate) or []) or "none"
            try:
                reply = ask(f"{symbol} {gate} = {value} as of {iso(at) if at else '?'} (evidence: {evidence}) [a/d/Enter]: ")
            except EOFError:
                say(f"Stopped; {written} checks saved.")
                return 0
            reply = reply.strip().lower()
            if reply not in ("a", "d", "agree", "disagree"):
                continue
            try:
                note = ask("  note: ")
            except EOFError:
                note = ""
            record_check(ctx.paths, week=week, symbol=symbol, as_of=ans.get("as_of"), gate=gate, scout=value,
                         owner="agree" if reply.startswith("a") else "disagree", note=note, now=ctx.clock.now())
            written += 1
    say(f"{written} checks saved to state/verify/checks.jsonl.")
    return 0
