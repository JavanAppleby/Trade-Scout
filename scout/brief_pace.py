"""The brief's "Polling pace" section (owner, 2026-11-10; DESIGN 21): how often Scout polls each
source, from the latest pace plan made by the brief's time (`scout.pace`, as-of rule), so the owner
can see each feed's spacing by hour, the fixed spacing it replaced and why it moved.

The spacing shown for an hour is the one in force at its half hour on a trading day (the hour turn
of `hour_turn_sources` is noted, not shown); "closed" is weekends and market holidays. The request
counts are estimates (one request a spacing; EDGAR counts each form).
"""

from __future__ import annotations

from datetime import time
from typing import Any

from scout.brief_sections import BriefInputs, md_escape
from scout.clock import et_date, fmt_et_bne
from scout.pace import CLOSED, FeedPlan, clamp, fixed_spacing, load_latest, requests_per_day

__all__ = ["fmt_spacing", "hour_ranges", "sec_pace", "slot_spacings"]

_ON_DEMAND_KINDS = ("reference", "macro", "vix")


def fmt_spacing(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:g} s"
    if seconds < 3600:
        return f"{round(seconds / 60.0, 1):g} min"
    return f"{round(seconds / 3600.0, 1):g} h"


def _active_at(cfg: Any, hour: int) -> bool:
    start, end = cfg.active_hours_et
    return start <= time(hour, 30) < end


def slot_spacings(cfg: Any, src: Any, plan: FeedPlan) -> tuple[list[float], list[float]]:
    """(paced, fixed) spacing for each slot: hours 0-23 of a trading day, then closed days."""
    paced, fixed = [], []
    for slot in range(CLOSED + 1):
        active = slot != CLOSED and _active_at(cfg, slot)
        base = fixed_spacing(src, active)
        fixed.append(base)
        paced.append(clamp(plan.wanted_s[slot], base, cfg.pace.slowest(src.name, plan.feed, active)))
    return paced, fixed


def hour_ranges(values: list[float], first_hour: int = 4) -> str:
    """"60 s 04:00-18:00, 30 min 18:00-04:00" for 24 hourly values, listed from `first_hour` (the start
    of active hours, so a trading day reads in order and a run may wrap midnight)."""
    if len(set(values)) == 1:
        return f"{fmt_spacing(values[0])} all day"
    runs: list[list[int]] = []  # [start hour, end hour (exclusive)]
    for hour in [*range(first_hour, 24), *range(first_hour)]:
        if runs and values[runs[-1][0]] == values[hour]:
            runs[-1][1] = hour + 1
        else:
            runs.append([hour, hour + 1])
    return ", ".join(f"{fmt_spacing(values[a])} {a:02d}:00-{b % 24:02d}:00" for a, b in runs)


def _fixed_text(src: Any) -> str:
    if not src.idle_spacing_s or src.idle_spacing_s == src.min_spacing_s:
        return fmt_spacing(src.min_spacing_s)
    return f"{fmt_spacing(src.min_spacing_s)} in trading hours, {fmt_spacing(src.idle_spacing_s)} otherwise"


def _unpaced(cfg: Any) -> list[str]:
    halts = [s.name for s in cfg.sources.values() if s.enabled and s.kind == "halt"]
    other = [s.name for s in cfg.sources.values() if s.enabled and s.kind in _ON_DEMAND_KINDS]
    out = []
    if halts:
        out.append(f"Not paced: {', '.join(halts)} (every 60 s in trading hours: a halt block lasts only as long "
                   "as the halt, so it is never slowed).")
    if other:
        out.append(f"Read at their publication times or on demand, as before: {', '.join(other)}.")
    return out


def sec_pace(inp: BriefInputs) -> list[str]:
    cfg = inp.cfg
    lines = ["## Polling pace", ""]
    if not cfg.pace.enabled:
        return lines + ["Pacing is off (`[pace] enabled = false`): every source polls at its fixed spacing.",
                        *_unpaced(cfg), ""]
    plan = load_latest(inp.paths, as_of=inp.as_of)
    if plan is None:
        return lines + ["No pace worked out yet: every source polls at its fixed spacing until the first "
                        f"recalculation (at start, then daily at {cfg.pace.recalc_time_et:%H:%M} ET).",
                        *_unpaced(cfg), ""]
    p = cfg.pace
    lines.append(f"Worked out {et_date(plan.made_at):%a %d %b} {fmt_et_bne(plan.made_at)} from Scout's polls "
                 f"since {et_date(plan.lookback_from):%a %d %b}. Each feed is polled about every {p.factor:g} x its "
                 "typical time between new items in that hour, never faster than its fixed spacing, never slower "
                 f"than its limit, and fast enough that the busiest burst seen fills at most {p.page_fill:.0%} of "
                 "its page. Hours are ET on trading days; closed = weekends and market holidays.")
    now_total = fixed_total = 0.0
    for (source, feed), fp in plan.feeds.items():
        src = cfg.sources.get(source)
        if src is None or not src.enabled:
            continue
        paced, fixed = slot_spacings(cfg, src, fp)
        now_total += requests_per_day(paced[:CLOSED])
        fixed_total += requests_per_day(fixed[:CLOSED])
        polls, hits = sum(fp.polls), sum(fp.hits)
        rate = f"new items in {hits / polls:.0%} of {polls:,} polls" if polls else "no polls measured"
        turn = " (30 s from :58 to :05 in trading hours)" if source in cfg.hour_turn_sources else ""
        name = md_escape(source if src.kind != "filing" else f"{source} {feed}", 60)
        lines.append(f"- {name}: {hour_ranges(paced[:CLOSED], cfg.active_hours_et[0].hour)}{turn}; closed "
                     f"{fmt_spacing(paced[CLOSED])} "
                     f"(fixed: {_fixed_text(src)}); {rate}.")
    lines.append(f"About {now_total:,.0f} requests a trading day for these feeds ({fixed_total:,.0f} at the fixed "
                 "spacing).")
    return lines + _unpaced(cfg) + [""]
