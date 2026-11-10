"""The brief's section on the thesis reads (READS_TEST.md; owner, 2026-11-10 and 2026-11-11).

One short section, only when `reads_enabled`: for the previous trading day and the brief's own day
so far, the reads by reader (first reads, rereads, strong ones (strength 4 or 5), skips by reason)
and the owner's notes; then the reads spend by reader, shown apart from the other Claude jobs'
spend, against the reads caps. With the market feed on (0.3.2), one line per day: "Market feed (SPY,
QQQ, IWM): 905 polls recorded, 3 too old, 4 failed; reads shown the market: R1 58 of 61, R2 9 of 9." Counts and money only: no read's text is shown, and a read is
evidence for the test, never advice.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from scout.calendar import previous_trading_day
from scout.clock import ManualClock
from scout.llm_records import calls_on
from scout.reads_budget import READS_JOB_PREFIX, ReadsBudget, reads_ledger_paths
from scout.reads_file import SKIP_WHYS, reads_on
from scout.reads_market import market_lines

__all__ = ["day_summary", "market_text", "sec_reads"]


def day_summary(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """reader -> {"first", "reread", "strong", "skips": {why: n}} (the owner under "owner")."""
    out: dict[str, dict[str, Any]] = {}
    for rec in lines:
        reader = str(rec.get("reader") or "?")
        s = out.setdefault(reader, {"first": 0, "reread": 0, "owner": 0, "strong": 0, "skips": Counter()})
        kind = rec.get("kind")
        if kind in ("first", "reread", "owner"):
            s[kind] += 1
            strength = rec.get("strength")
            if kind != "reread" and isinstance(strength, int) and strength >= 4:
                s["strong"] += 1
        elif kind == "skip":
            s["skips"][str(rec.get("why"))] += 1
    return out


def _reader_text(reader: str, s: dict[str, Any]) -> str:
    if reader == "owner":
        return f"owner {s['owner']} (strong {s['strong']})"
    skips = ", ".join(f"{w} {s['skips'][w]}" for w in SKIP_WHYS if s["skips"].get(w)) or "none"
    return f"{reader}: first {s['first']} (strong {s['strong']}), rereads {s['reread']}, skips {skips}"


def market_text(paths: Any, day: date, read_lines: list[dict[str, Any]], symbols: Any) -> str:
    """The day's market-feed line (0.3.2): polls recorded, too old and failed (from the market file),
    and per reader how many of its reads were shown the market."""
    recorded = too_old = failed = 0
    for rec in market_lines(paths, day):
        if rec.get("kind") == "market":
            recorded += 1
        elif rec.get("kind") == "miss":
            too_old, failed = (too_old + 1, failed) if rec.get("reason") == "stale" else (too_old, failed + 1)
    shown: dict[str, list[int]] = {}
    for rec in read_lines:
        if rec.get("kind") in ("first", "reread"):
            s = shown.setdefault(str(rec.get("reader") or "?"), [0, 0])
            s[0] += 1 if rec.get("market_shown") else 0
            s[1] += 1
    readers = ", ".join(f"{r} {s[0]} of {s[1]}" for r, s in sorted(shown.items())) or "no reads"
    return (f"  Market feed ({', '.join(symbols)}): {recorded} polls recorded, {too_old} too old, {failed} failed; "
            f"reads shown the market: {readers}.")


def _spend(paths: Any, day: date) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in calls_on(reads_ledger_paths(paths), day):  # state/reads/ledger: reads calls only
        job = str(r.get("job"))
        if job.startswith(READS_JOB_PREFIX):
            rid = job[len(READS_JOB_PREFIX):]
            try:
                out[rid] = out.get(rid, 0.0) + float(r.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                continue
    return out


def sec_reads(inp: Any) -> list[str]:
    cfg = inp.cfg
    if not cfg.reads.enabled:
        return []
    lines = ["## Thesis reads (a paper test: no bot acts on a read)", ""]
    for day in (previous_trading_day(inp.day), inp.day):
        day_lines = reads_on(inp.paths, day)
        summary = day_summary(day_lines)
        label = f"{day.isoformat()}" + (" so far" if day == inp.day else "")
        if not summary:
            lines.append(f"- {label}: no reads.")
            if cfg.reads.market_enabled:
                lines.append(market_text(inp.paths, day, day_lines, cfg.reads.market_symbols))
            continue
        parts = [_reader_text(r, summary[r]) for r in sorted(summary, key=lambda r: (r == "owner", r))]
        lines.append(f"- {label}: " + "; ".join(parts) + ".")
        spend = _spend(inp.paths, day)
        if spend:
            by = ", ".join(f"{r} ${v:.4f}" for r, v in sorted(spend.items()))
            lines.append(f"  Reads spend ${sum(spend.values()):.4f} ({by}) of ${cfg.reads.daily_cost_cap_usd:.2f} a day.")
        if cfg.reads.market_enabled:
            lines.append(market_text(inp.paths, day, day_lines, cfg.reads.market_symbols))
    month = ReadsBudget(inp.paths, cfg, ManualClock(inp.as_of)).reads_status().get("spent_month_usd")
    month_text = f"${month:.4f}" if isinstance(month, (int, float)) else "unknown (the ledger could not be read)"
    lines.append(f"Reads spend this month {month_text} of ${cfg.reads.monthly_cost_cap_usd:.2f}, kept apart from the "
                 "Claude jobs' spend above (the two budgets never stop each other).")
    return lines + [""]
