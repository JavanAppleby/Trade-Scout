"""`python -m scout answers SYMBOL... [--as-of ISO-UTC]` (DESIGN 2.5, 8).

Prints the six reader answers for each symbol at `--as-of` (default: now), from Scout's own
folder, exactly as Radar and Pilot would read them. It writes nothing: no answer, no due record,
no file of any kind. Exit codes: 0 printed, 2 bad input (a naive or unreadable `--as-of`).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, TextIO

from scout_reader.api import JASON_TAGS, MARKET_FIELDS, Reader, hold_allowed

from scout.clock import fmt_et_bne, iso

__all__ = ["cli_answers", "parse_as_of", "render"]


def parse_as_of(text: str | None, now: datetime) -> datetime | None:
    """An aware time from `text` (ISO 8601 with Z or an offset); `now` when empty; None when bad."""
    if text is None or not str(text).strip():
        return now
    value = str(text).strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _line(name: str, res: object) -> str:
    if not isinstance(res, Mapping):
        return f"  {name:<28} unknown (no answer)"
    value = res.get("value", "unknown")
    parts = [f"  {name:<28} {value}"]
    if res.get("stale"):
        parts.append("[stale]")
    reasons = res.get("reasons") or []
    if reasons:
        parts.append("reasons: " + ", ".join(str(r) for r in reasons))
    evidence = res.get("evidence") or []
    if evidence:
        parts.append("evidence: " + ", ".join(str(e) for e in evidence))
    if res.get("classes"):
        parts.append("classes: " + ", ".join(str(c) for c in res["classes"]))
    return "  ".join(parts)


def render(reader: Reader, symbols: Sequence[str], as_of: datetime) -> list[str]:
    """The printed lines (pure apart from reading Scout's files)."""
    lines = [f"Scout answers as of {iso(as_of)} ({fmt_et_bne(as_of)}). Record only; Scout never trades."]
    context: Any = reader.market_context(as_of)
    lines.append("Market:")
    for name in MARKET_FIELDS:
        lines.append(_line(name, context.get(name) if isinstance(context, Mapping) else None))
    for symbol in symbols:
        sym = symbol.strip().upper()
        lines.append(f"{sym}:")
        g6 = reader.fresh_catalyst(sym, as_of)
        g5 = reader.event_before_next_session(sym, as_of)
        lines.append(_line("fresh_catalyst (gate 6)", g6))
        lines.append(_line("event_before_next_session", g5))
        lines.append(_line("news_headline", reader.news_headline(sym, as_of)))
        lines.append(_line("halt_open", reader.halt_open(sym, as_of)))
        tags = reader.jason_tags(sym, None, as_of)
        for name in JASON_TAGS:
            lines.append(_line(name, tags.get(name) if isinstance(tags, Mapping) else None))
        allowed = "yes" if hold_allowed(g6, g5) else "no"
        lines.append(f"  {'gates 5 and 6 allow a hold':<28} {allowed} (needs gate 6 true, gate 5 false, fresh)")
    return lines


def cli_answers(ctx: Any, args: argparse.Namespace, out: TextIO | None = None) -> int:
    now = ctx.clock.now()
    as_of = parse_as_of(getattr(args, "as_of", None), now)
    if as_of is None:
        print("Scout: --as-of must be a time with a zone, e.g. 2026-11-03T00:45:00Z", file=out)
        return 2
    symbols = [s for s in (getattr(args, "symbols", None) or []) if s and s.strip()]
    if not symbols:
        print("Scout: give at least one symbol", file=out)
        return 2
    reader = Reader(ctx.paths.root)
    for line in render(reader, symbols, as_of):
        print(line, file=out)
    return 0
