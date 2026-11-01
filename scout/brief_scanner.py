"""The brief's "Paper Gains scanners" sub-section of the Discord section (DESIGN 2.7). Display only.

From the `scanner` facts the `discord_inbox` job stored (one per ticker of a #15m-scanner,
#daily-scanner or #weekly-scanner card), with the as-of rule, since the prior close: grouped by
timeframe (15m, daily, weekly) and then by pattern and state as printed, newest group first, at most
`discord_brief_max_items` groups per timeframe and `discord_scanner_brief_max_tickers` tickers per
group. A ticker that is a recent Radar find or a watch symbol is flagged and listed first. Every
string goes through `md_escape`. Record and brief only: no gate reads a scanner fact.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from scout.brief_sections import BriefInputs, collect_facts, md_escape
from scout.calendar import prior_close_before
from scout.clock import fmt_et_bne, parse_iso
from scout.discord_ingest import SCANNER_KIND
from scout.discord_parse import SCANNER_TIMEFRAMES

__all__ = ["groups", "sec_scanners"]

TIMEFRAMES = tuple(SCANNER_TIMEFRAMES.values())  # 15m, daily, weekly


def groups(facts: list[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """timeframe -> [{pattern, state, tickers (newest alert first), posts, latest}], newest group first."""
    by: dict[tuple[str, str, str | None], dict[str, Any]] = {}

    def order(f: Mapping[str, Any]) -> tuple[str, str, int]:  # newest card first, then the card's own order
        extra = f.get("extra") if isinstance(f.get("extra"), Mapping) else {}
        printed = extra.get("tickers") if isinstance(extra.get("tickers"), list) else []
        place = printed.index(f.get("ticker")) if f.get("ticker") in printed else len(printed)
        return str(f.get("published_at")), str(f.get("guid")), -place

    ordered = sorted(facts, key=order, reverse=True)
    for f in ordered:
        extra = f.get("extra") if isinstance(f.get("extra"), Mapping) else {}
        tf, pattern, state = extra.get("timeframe"), extra.get("pattern"), extra.get("state")
        if tf not in TIMEFRAMES or not isinstance(pattern, str) or not f.get("ticker"):
            continue
        key = (str(tf), pattern, state if isinstance(state, str) else None)
        g = by.setdefault(key, {"pattern": key[1], "state": key[2], "tickers": [], "posts": set(),
                                "latest": f.get("published_at")})
        if f["ticker"] not in g["tickers"]:
            g["tickers"].append(f["ticker"])
        g["posts"].add(str(f.get("guid")))
    out: dict[str, list[dict[str, Any]]] = {tf: [] for tf in TIMEFRAMES}
    for (tf, _p, _s), g in by.items():
        out[tf].append(g)
    return out


def _flags(inp: BriefInputs) -> dict[str, str]:
    out = {s.upper(): "watch symbol" for s in inp.cfg.watch_symbols}
    for f in inp.finds:
        out[f.symbol.upper()] = "Radar find"
    return out


def sec_scanners(inp: BriefInputs) -> list[str]:
    since = prior_close_before(inp.as_of)
    rows = [f for f in collect_facts(inp, since, inp.as_of, (SCANNER_KIND,))
            if (parse_iso(f.get("published_at")) or since) >= since]
    lines = ["Paper Gains scanners since the prior close (record only; no gate reads them):"]
    if not rows:
        return lines + ["- none captured", ""]
    flags = _flags(inp)
    n, cap = inp.cfg.discord_brief_max_items, inp.cfg.discord_scanner_brief_max_tickers
    for tf, items in groups(rows).items():
        for g in items[:n]:
            tickers = sorted(g["tickers"], key=lambda t: t not in flags)  # stable: flagged first, else newest first
            shown = [f"**{md_escape(t, 12)}** ({flags[t]})" if t in flags else md_escape(t, 12)
                     for t in tickers[:cap]]
            more = f", +{len(tickers) - cap} more" if len(tickers) > cap else ""
            state = f" ({md_escape(g['state'], 40)})" if g["state"] else ""
            at = parse_iso(g["latest"])
            when = f", latest {fmt_et_bne(at)}" if at else ""
            lines.append(f"- {tf}: {md_escape(g['pattern'], 60)}{state}: {', '.join(shown)}{more} "
                         f"({len(g['posts'])} card(s){when})")
        if len(items) > n:
            lines.append(f"- {tf}: ... and {len(items) - n} more pattern(s) in state/facts.")
    return lines + [""]
