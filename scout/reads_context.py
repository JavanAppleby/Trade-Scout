"""Scout's own facts for one read (READS_TEST.md 5.2), as of the moment the read is prepared.

- news and filings for the symbol first seen in the last `reads_news_lookback_h` hours (wire,
  filing and Discord facts), newest first, at most `reads_news_max_items`, each with Scout's
  verified label (J1 or J3) when there is one;
- the `entry_blocks` answer (through `scout_reader`, as Radar asks it) and whether a CPI, FOMC or
  jobs window is open (a `macro_window` block);
- Jason's current setups for the symbol, from Scout's level snapshot.

Only the ids of the facts shown are recorded with a read (`fact_ids`), never their text. Nothing
here writes anything, and nothing that answers a question reads the reads files.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from scout_reader import records as R
from scout_reader.api import Reader
from scout_reader.jason import current_for, latest_snapshot

from scout.clock import et_date, parse_iso
from scout.config import ScoutConfig
from scout.paths import ScoutPaths
from scout.store import read_jsonl

NEWS_KINDS = ("wire", "filing", "discord")

__all__ = ["NEWS_KINDS", "scout_context"]


def _days(now: datetime, hours: float) -> list:
    first = et_date(now - timedelta(hours=hours))
    out, day = [], et_date(now)
    while day >= first:
        out.append(day)
        day -= timedelta(days=1)
    return out


def _labels(paths: ScoutPaths, days: list, fact_ids: set[str], now: datetime) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for day in sorted(days):
        for rec in read_jsonl(paths.day_file(paths.labels, day)):
            fid = rec.get("fact_id")
            at = parse_iso(rec.get("labelled_at"))
            if fid in fact_ids and rec.get("verified") is True and at is not None and at <= now:
                out[str(fid)] = {"catalyst_type": rec.get("catalyst_type"), "direction": rec.get("direction")}
    return out


def _news(cfg: ScoutConfig, paths: ScoutPaths, symbol: str, now: datetime) -> list[dict[str, Any]]:
    hours = cfg.reads.news_lookback_h
    start = now - timedelta(hours=hours)
    days = _days(now, hours)
    rows = []
    for day in days:
        for rec in read_jsonl(paths.day_file(paths.facts, day)):
            if rec.get("kind") not in NEWS_KINDS or str(rec.get("ticker") or "").upper() != symbol:
                continue
            seen = parse_iso(rec.get("first_seen_at"))
            if seen is None or not (start <= seen <= now) or not rec.get("fact_id"):
                continue
            rows.append((seen, rec))
    rows.sort(key=lambda x: x[0], reverse=True)
    rows = rows[: cfg.reads.news_max_items]
    labels = _labels(paths, days, {str(r.get("fact_id")) for _s, r in rows}, now)
    out = []
    for seen, rec in rows:
        fid = str(rec.get("fact_id"))
        out.append({"fact_id": fid, "source": rec.get("source"), "kind": rec.get("kind"), "title": rec.get("title"),
                    "age_min": int(math.floor((now - seen).total_seconds() / 60)), "label": labels.get(fid)})
    return out


def _jason(paths: ScoutPaths, symbol: str, now: datetime) -> list[dict[str, Any]]:
    snap = latest_snapshot(read_jsonl(paths.root / R.JASON_SNAPSHOTS), now)
    if not snap:
        return []
    out = []
    for s in current_for(snap, symbol, et_date(now).isoformat())[:3]:
        levels = [{"kind": lv.get("kind"), "price": lv.get("price"), "note": lv.get("note")}
                  for lv in s.get("levels") or () if isinstance(lv, Mapping)]
        out.append({"direction": s.get("direction"), "expires": s.get("expires"), "levels": levels})
    return out


def scout_context(cfg: ScoutConfig, paths: ScoutPaths, symbol: str, now: datetime,
                  reader: Reader | None = None) -> dict[str, Any]:
    """Scout's facts for `symbol` at `now`. Never raises: a part that fails is left out ("unknown")."""
    sym = symbol.strip().upper()
    ctx: dict[str, Any] = {"news": [], "entry_blocks": {"value": "unknown", "blocks": []},
                           "macro_window": "unknown", "jason": [], "fact_ids": []}
    try:
        ctx["news"] = _news(cfg, paths, sym, now)
    except Exception:  # noqa: BLE001 - a read goes ahead without news rather than not at all
        ctx["news"] = []
    eb = (reader or Reader(paths.root)).entry_blocks(sym, now)  # never raises
    blocks = [b for b in eb.get("blocks") or [] if isinstance(b, Mapping)]
    ctx["entry_blocks"] = {"value": eb.get("value", "unknown"), "blocks": blocks}
    if any(b.get("kind") == "macro_window" for b in blocks):
        ctx["macro_window"] = "yes"
    elif eb.get("value") == "false":
        ctx["macro_window"] = "no"
    try:
        ctx["jason"] = _jason(paths, sym, now)
    except Exception:  # noqa: BLE001
        ctx["jason"] = []
    ids = [n["fact_id"] for n in ctx["news"]] + [str(b.get("fact_id")) for b in blocks if b.get("fact_id")]
    ctx["fact_ids"] = list(dict.fromkeys(ids))
    return ctx
