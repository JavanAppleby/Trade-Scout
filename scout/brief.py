"""The morning brief (SCOUT_SPEC 10.2, DESIGN 2.6): `reports/scout-<ET date>.md`.

A deterministic skeleton is always written: macro events credited to Nowflation with a link, the
inflation regime and VIX, overnight catalysts, open halts, earnings due windows among recent finds,
Jason's current setups and frame (or "no current levels file"), source health, LLM spend (or "LLM
cap reached"), and the week's verification sample. Times are ET and Brisbane; every piece of
third-party text is escaped. J4's paragraph is appended only when the LLM is on and the call works.
A brief for a past date is built as of that date's `brief_time_et` (as-of rule).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from scout.brief_sections import (
    BriefInputs, catalysts, md_escape, open_halts, sec_calendar, sec_catalysts, sec_due_windows, sec_halts,
    sec_header, sec_health, sec_jason, sec_llm, sec_macro, sec_regime, sec_verify,
)
from scout.calendar import previous_trading_day
from scout.clock import et_date, et_datetime
from scout.inputs import radar_finds
from scout.llm_ledger import Ledger
from scout.store import atomic_write_text

SECTIONS = (sec_header, sec_calendar, sec_macro, sec_regime, sec_catalysts, sec_halts, sec_due_windows, sec_jason,
            sec_health, sec_llm, sec_verify)

__all__ = ["SECTIONS", "brief_path", "build_brief", "cli_brief", "collect", "j4_items", "write_brief"]


def brief_path(ctx: Any, day: date) -> Path:
    return ctx.paths.reports / f"scout-{day.isoformat()}.md"


def _as_of(ctx: Any, day: date, now: datetime) -> datetime:
    if et_date(now) == day:
        return now
    at = et_datetime(day, ctx.cfg.brief_time_et)
    return min(at, now)


def collect(ctx: Any, day: date, now: datetime) -> BriefInputs:
    cfg = ctx.cfg
    as_of = _as_of(ctx, day, now)
    finds: list[Any] = []
    skipped = not cfg.radar_dir.strip()
    if not skipped:
        for d in (previous_trading_day(day), day):
            finds.extend(f for f in radar_finds(cfg, d))
    llm = Ledger(ctx.paths, cfg, ctx.clock).status() if cfg.llm_enabled else {}
    return BriefInputs(cfg=cfg, paths=ctx.paths, day=day, as_of=as_of, llm=llm, finds=finds, radar_skipped=skipped)


def render(inp: BriefInputs) -> str:
    lines: list[str] = []
    for section in SECTIONS:
        lines.extend(section(inp))
    return "\n".join(lines).rstrip() + "\n"


def build_brief(ctx: Any, day: date, now: datetime) -> str:
    """The deterministic skeleton (no LLM call)."""
    return render(collect(ctx, day, now))


def j4_items(inp: BriefInputs) -> list[dict[str, Any]]:
    """Public items for J4: catalysts (C<n>), open halts (H<n>), macro event lines (M<n>)."""
    items: list[dict[str, Any]] = []
    for n, f in enumerate(catalysts(inp)[:40], start=1):
        items.append({"id": f"C{n}", "ticker": f.get("ticker"), "fact_id": f.get("fact_id"),
                      "text": f"{f.get('ticker')} [{f.get('class')}] {f.get('title') or ''}"})
    for n, h in enumerate(open_halts(inp)[:10], start=1):
        items.append({"id": f"H{n}", "ticker": h.get("ticker"), "fact_id": h.get("fact_id"),
                      "text": f"{h.get('ticker')} trading halt, code {h.get('halt_code')}"})
    for n, line in enumerate((x for x in sec_macro(inp) if x.startswith("- ")), start=1):
        items.append({"id": f"M{n}", "text": line[2:].replace("\\", "")})
    return items


def write_brief(ctx: Any, day: date, now: datetime, *, transport: Any = None) -> Path:
    inp = collect(ctx, day, now)
    text = render(inp)
    if ctx.cfg.llm_enabled:
        try:
            from scout.llm_j4 import J4Job  # noqa: PLC0415

            tr = transport if transport is not None else ctx.llm_transport()
            if tr is not None:
                job = J4Job(ctx.cfg, ctx.paths, tr, Ledger(ctx.paths, ctx.cfg, ctx.clock), ctx.clock, log=ctx.log)
                para = job.paragraph(j4_items(inp), now)
                if para:
                    text = text.rstrip() + "\n\n" + para
        except Exception as exc:  # noqa: BLE001 - the deterministic brief is always written
            ctx.log.warning("J4 paragraph skipped: %s", type(exc).__name__)
    target = brief_path(ctx, day)
    atomic_write_text(ctx.paths, target, text)
    return target


def cli_brief(ctx: Any, args: argparse.Namespace) -> int:
    now = ctx.clock.now()
    raw = getattr(args, "date", None)
    try:
        day = date.fromisoformat(raw) if raw else et_date(now)
    except ValueError:
        print(f"Scout: --date must be YYYY-MM-DD, got {md_escape(raw, 20)}")
        return 2
    if day > et_date(now) + timedelta(days=1):
        print("Scout: --date is in the future")
        return 2
    path = write_brief(ctx, day, now)
    print(f"Brief written: {path}")
    return 0
