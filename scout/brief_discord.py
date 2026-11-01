"""The brief's "Paper Gains Discord" section (DESIGN 2.7). Display only, never parsed.

Everything shown comes from Scout's own files with the as-of rule: the facts the `discord_inbox` job
stored (kinds `discord` and `earnings_schedule`), the Jason snapshot (for today's daily card, added as
a `daily` level file), the capture bookkeeping and rejects, and the heartbeat. Discord text is
untrusted third-party text transcribed by an LLM task: every piece goes through `md_escape` (one line,
cut, no markup, no HTML).

Lists (each newest first, at most `discord_brief_max_items`):
- today's daily card: the symbols and levels of today's `daily` level file;
- earnings lists for today and the next trading day (pre-market, after the close, time unknown);
- small-cap catalysts (`discord_catalyst_channels`) since the prior close; a ticker a Radar find
  prices at or under `scan_max_price` is flagged (Scout has no price feed of its own, DESIGN D8);
- market-wide headlines (`discord_headline_channels`) since the prior close;
- earnings reactions (`discord_reaction_channels`) naming a recent Radar find or a watch symbol;
- Paper Gains scanners (kind `scanner`, `scout.brief_scanner`): alerts since the prior close by
  timeframe and pattern, Radar finds and watch symbols flagged;
- capture health: `discord_inbox` on the heartbeat, each channel's newest capture, today's refused lines.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from scout_reader import records as R

from scout.brief_scanner import sec_scanners
from scout.brief_sections import BriefInputs, collect_facts, md_escape
from scout.calendar import next_trading_day, prior_close_before
from scout.clock import et_date, fmt_et_bne, parse_iso
from scout.discord_ingest import REJECTS_DIR, STATUS_SOURCE, last_captures
from scout.store import read_json, read_jsonl

__all__ = ["posts", "sec_discord"]

SESSION_NAMES = (("pre", "pre-market (AM)"), ("post", "after the close (PM)"), ("unknown", "time not given"))
#: Schedule facts are read this far back: a weekly calendar lists dates up to about two weeks ahead.
_DAYS_BACK = timedelta(days=21)
#: The brief names at most this many distinct refusal reasons a day (the rest are counted).
MAX_REASONS = 6
_CARD_KINDS = (("trigger", "long trigger"), ("target", "long targets"), ("invalidation", "short trigger"),
               ("support", "short targets"), ("resistance", "resistance"), ("re-entry", "re-entry"))


def posts(facts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One entry per (channel, message id) from the per-ticker `discord` facts, newest posted first."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for f in facts:
        if f.get("kind") != "discord":
            continue
        key = (str(f.get("feed")), str(f.get("guid")))
        entry = by_key.setdefault(key, {"fact": f, "tickers": []})
        if f.get("ticker") and f["ticker"] not in entry["tickers"]:
            entry["tickers"].append(f["ticker"])
    out = list(by_key.values())
    out.sort(key=lambda e: (str(e["fact"].get("published_at")), str(e["fact"].get("guid"))), reverse=True)
    return out


def _when(stamp: object) -> str:
    at = parse_iso(stamp)
    return f"{et_date(at):%a %d %b} {fmt_et_bne(at)}" if at else "time unknown"


def _line(entry: Mapping[str, Any], flags: Mapping[str, str] | None = None) -> str:
    f = entry["fact"]
    extra = f.get("extra") if isinstance(f.get("extra"), Mapping) else {}
    text = extra.get("text") or f.get("title") or ""
    tickers = ", ".join(md_escape(t, 12) + (f" ({flags[t]})" if flags and t in flags else "")
                        for t in entry["tickers"])
    head = f"**{tickers}** " if tickers else ""
    return f"- {_when(f.get('published_at'))} {head}{md_escape(text, 220)} ({md_escape(f.get('feed'), 24)})"


def _snapshot_daily(inp: BriefInputs) -> list[Mapping[str, Any]]:
    best = None
    for rec in read_jsonl(inp.root / R.JASON_SNAPSHOTS):
        if R.known(rec) and R.visible(rec, inp.as_of):
            best = rec
    day = inp.day.isoformat()
    return [s for s in (best or {}).get("setups") or [] if isinstance(s, Mapping)
            and s.get("source_kind") == "daily" and str(s.get("session_date")) == day]


def _card(inp: BriefInputs, recent: list[dict[str, Any]]) -> list[str]:
    lines = [f"Daily watchlist card for {inp.day:%a %d %b} (from the added level file):"]
    setups = _snapshot_daily(inp)
    for s in setups[: inp.cfg.discord_brief_max_items]:
        bits = []
        for kind, label in _CARD_KINDS:
            readings = ["/".join(md_escape(r, 12) for r in lv.get("readings") or [])
                        for lv in s.get("levels") or [] if isinstance(lv, Mapping) and lv.get("kind") == kind]
            if readings:
                bits.append(f"{label} {', '.join(readings)}")
        lines.append(f"- **{md_escape(s.get('symbol'), 12)}**: " + ("; ".join(bits) or "no levels"))
    if not setups:
        cards = [e for e in recent if (e["fact"].get("extra") or {}).get("post_kind") == "levels_card"
                 and parse_iso(e["fact"].get("published_at")) is not None
                 and et_date(parse_iso(e["fact"].get("published_at"))) == inp.day]
        lines.append("- No daily level file for today." + (
            " A card post was captured, but no level file was added (see inbox\\jason and its .rejected.txt notes)."
            if cards else ""))
    return lines + [""]


def _earnings(inp: BriefInputs) -> list[str]:
    days = [inp.day, next_trading_day(inp.day)]
    start = inp.as_of.replace(microsecond=0) - _DAYS_BACK
    rows = collect_facts(inp, start, inp.as_of, ("earnings_schedule",))
    lines = ["Earnings lists posted on Discord (block-only input to gate 5):"]
    for day in days:
        groups: dict[str, list[str]] = {}
        channels: set[str] = set()
        for f in rows:
            extra = f.get("extra") if isinstance(f.get("extra"), Mapping) else {}
            if extra.get("earnings_date") != day.isoformat() or not f.get("ticker"):
                continue
            names = groups.setdefault(str(extra.get("session")), [])
            if f["ticker"] not in names:
                names.append(f["ticker"])
            channels.add(str(f.get("feed")))
        bits = [f"{label}: {', '.join(md_escape(t, 12) for t in sorted(groups[key]))}"
                for key, label in SESSION_NAMES if groups.get(key)]
        source = f" (from {', '.join(md_escape(c, 24) for c in sorted(channels))})" if channels else ""
        lines.append(f"- {day:%a %d %b}: " + ("; ".join(bits) + source if bits else "none listed"))
    return lines + [""]


def _recent_symbols(inp: BriefInputs) -> set[str]:
    return {f.symbol.upper() for f in inp.finds} | {s.upper() for s in inp.cfg.watch_symbols}


def _cheap(inp: BriefInputs) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in inp.finds:
        if f.price is not None and f.price <= inp.cfg.scan_max_price:
            out[f.symbol.upper()] = f"Radar find ${f.price:g}, under ${inp.cfg.scan_max_price:g}"
    return out


def _list(title: str, entries: list[dict[str, Any]], limit: int, empty: str,
          flags: Mapping[str, str] | None = None) -> list[str]:
    lines = [title]
    lines += [_line(e, flags) for e in entries[:limit]] or [f"- {empty}"]
    if len(entries) > limit:
        lines.append(f"- ... and {len(entries) - limit} more in state/facts.")
    return lines + [""]


def _health(inp: BriefInputs) -> list[str]:
    lines = ["Capture health:"]
    hb = read_json(inp.paths.heartbeat_file) or {}
    st = (hb.get("sources") or {}).get(STATUS_SOURCE) if isinstance(hb.get("sources"), Mapping) else None
    if isinstance(st, Mapping):
        why = f" ({md_escape(st.get('last_error'), 60)})" if st.get("last_error") else ""
        lines.append(f"- discord_inbox: {md_escape(st.get('status'), 12)}{why}; it is never read by a gate.")
    else:
        lines.append("- discord_inbox: not on the heartbeat yet.")
    caps = last_captures(inp.paths, inp.as_of)
    for channel in inp.cfg.discord_channels:
        rec = caps.get(channel)
        lines.append(f"- #{md_escape(channel, 24)}: " + (f"last capture {_when(rec.get('capture_at'))}" if rec
                                                         else "no capture yet"))
    rejects = [r for r in read_jsonl(inp.paths.day_file(inp.paths.discord / REJECTS_DIR, et_date(inp.as_of)))
               if (parse_iso(r.get("at")) or inp.as_of) <= inp.as_of]
    if rejects:
        reasons: dict[str, int] = {}
        for r in rejects:
            reasons[str(r.get("reason"))] = reasons.get(str(r.get("reason")), 0) + 1
        top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        shown = [f"{md_escape(k, 40)} x{v}" for k, v in top[:MAX_REASONS]]
        if len(top) > MAX_REASONS:
            shown.append(f"+{len(top) - MAX_REASONS} other reason(s)")
        lines.append(f"- Refused lines today: {len(rejects)} (" + ", ".join(shown) + ")")
    return lines + [""]


def sec_discord(inp: BriefInputs) -> list[str]:
    cfg = inp.cfg
    lines = ["## Paper Gains Discord", ""]
    if not cfg.discord_enabled:
        return lines + ["Discord captures are off (discord_enabled = false).", ""]
    lines += ["Third-party text captured by the owner's scheduled Claude task; shown as data, never as advice. "
              "Only a listed earnings date reaches an answer, and only to block a hold (gate 5).", ""]
    since = prior_close_before(inp.as_of)
    recent = posts(collect_facts(inp, since, inp.as_of, ("discord",)))
    recent = [e for e in recent if (parse_iso(e["fact"].get("published_at")) or since) >= since]
    n = cfg.discord_brief_max_items
    lines += _card(inp, recent)
    lines += _earnings(inp)
    by = {name: [e for e in recent if e["fact"].get("feed") in getattr(cfg, name)]
          for name in ("discord_catalyst_channels", "discord_headline_channels", "discord_reaction_channels")}
    lines += _list("Small-cap catalysts since the prior close:", by["discord_catalyst_channels"], n,
                   "none captured", _cheap(inp))
    lines += _list("Market headlines since the prior close:", by["discord_headline_channels"], n, "none captured")
    watched = _recent_symbols(inp)
    reactions = [e for e in by["discord_reaction_channels"] if watched & set(e["tickers"])]
    lines += _list("Earnings reactions naming a recent Radar find or a watch symbol:", reactions, n,
                   "none" if watched else "no Radar finds or watch symbols to match")
    lines += sec_scanners(inp)
    lines += _health(inp)
    return lines
