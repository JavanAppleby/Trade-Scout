"""The brief's sections (SCOUT_SPEC 5.5, 7.3, 10.2; DESIGN 2.6). One function per section, each
`(BriefInputs) -> list[str]` of Markdown lines. Files are read only through `scout_reader.records`
and `scout.store`, with the as-of rule (`records.visible`), so a brief for a past date shows only
what Scout had seen by then. All third-party text goes through `md_escape`. Times show ET and
Brisbane. The brief is for the owner and never parsed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from scout_reader import records as R

from scout.calendar import calendar_covers, prior_close_before
from scout.clock import et_date, fmt_et_bne, parse_iso
from scout.config import ScoutConfig
from scout.macro import LEAN_NOTE, daily_as_of
from scout.paths import ScoutPaths
from scout.store import read_json, read_jsonl

NOWFLATION_CREDIT = "Macro data: [Nowflation](https://nowflation.com) (credited; raw files are never republished)."
MAX_CATALYSTS = 40
MAX_SETUPS = 30
_MD = str.maketrans({c: "\\" + c for c in "\\`*_{}[]()#+!|~"})

__all__ = [
    "MAX_CATALYSTS", "NOWFLATION_CREDIT", "BriefInputs", "catalysts", "collect_facts", "fold_offering_filings",
    "iso_week", "md_escape",
    "open_halts", "sec_calendar", "sec_catalysts", "sec_due_windows", "sec_halts", "sec_header", "sec_health",
    "sec_jason", "sec_llm", "sec_macro", "sec_regime", "sec_verify",
]


def md_escape(text: object, limit: int = 300) -> str:
    """Third-party text made safe for a Markdown report: one line, no markup, no HTML."""
    s = " ".join(str("" if text is None else text).split())
    if len(s) > limit:
        s = s[:limit] + "..."
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.translate(_MD)


def iso_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _when(stamp: object) -> str:
    at = parse_iso(stamp)
    return fmt_et_bne(at) if at else "time unknown"


def _when_day(stamp: object) -> str:
    at = parse_iso(stamp)
    return f"{et_date(at):%a %d %b} {fmt_et_bne(at)}" if at else "time unknown"


@dataclass
class BriefInputs:
    cfg: ScoutConfig
    paths: ScoutPaths
    day: date
    as_of: datetime
    llm: Mapping[str, Any] = field(default_factory=dict)  # ledger status
    finds: list[Any] = field(default_factory=list)  # RadarFind, today's and the prior trading day's
    radar_skipped: bool = False

    @property
    def root(self) -> Path:
        return self.paths.root


# --------------------------------------------------------------------------- shared reads


def collect_facts(inp: BriefInputs, start: datetime, end: datetime, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for path in R.day_files(inp.paths.facts, start, end):
        for rec in read_jsonl(path):
            if not R.known(rec) or rec.get("kind") not in kinds or not R.visible(rec, inp.as_of):
                continue
            seen = parse_iso(rec.get("first_seen_at"))
            if seen is not None and start <= seen <= end:
                out.append(rec)
    out.sort(key=lambda r: (str(r.get("first_seen_at")), str(r.get("fact_id"))))
    return out


def _labels(inp: BriefInputs, start: datetime) -> dict[str, dict[str, Any]]:
    """Latest visible J1 label per fact id (shown beside a catalyst, never used to choose one)."""
    out: dict[str, dict[str, Any]] = {}
    for path in R.day_files(inp.paths.labels, start, inp.as_of):
        for rec in read_jsonl(path):
            if R.known(rec) and rec.get("job") == "J1" and rec.get("fact_id") and R.visible(rec, inp.as_of):
                out[str(rec["fact_id"])] = rec
    return out


def _too_dear(inp: BriefInputs) -> set[str]:
    return {f.symbol for f in inp.finds if f.price is not None and f.price > inp.cfg.scan_max_price}


def _published(rec: Mapping[str, Any]) -> datetime | None:
    return parse_iso(rec.get("published_at"))


def catalysts(inp: BriefInputs) -> list[dict[str, Any]]:
    """Wire and filing facts with a ticker and a class, published and first seen since the prior
    close (gate 6's rule), newest first (DESIGN D8: a ticker is left out only when a Radar find
    prices it above `scan_max_price`). Oldest first with a cap of 40 showed the first half hour
    after the close and hid every overnight and pre-market item (third review); the published-at
    window keeps a first poll's backlog from the previous day out."""
    start = prior_close_before(inp.as_of)
    dear = _too_dear(inp)
    out = []
    for f in collect_facts(inp, start, inp.as_of, ("wire", "filing")):
        published = _published(f)
        if published is None or not start <= published <= inp.as_of:
            continue
        if f.get("ticker") and f.get("class") and str(f.get("ticker")).upper() not in dear:
            out.append(f)
    out.sort(key=lambda r: (_published(r), str(r.get("first_seen_at")), str(r.get("fact_id"))), reverse=True)
    return out


def fold_offering_filings(facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One line per issuer for `offering` filings (a bank files 424B2 notes all day, one fact per
    listed ticker): the newest is kept, and `folded[fact_id]` counts the others it stands for."""
    shown: list[dict[str, Any]] = []
    folded: dict[str, int] = {}
    first: dict[str, str] = {}
    for f in facts:
        if f.get("kind") == "filing" and f.get("class") == "offering":
            issuer = str(f.get("cik") or f.get("ticker"))
            if issuer in first:
                folded[first[issuer]] = folded.get(first[issuer], 0) + 1
                continue
            first[issuer] = str(f.get("fact_id"))
        shown.append(f)
    return shown, folded


def open_halts(inp: BriefInputs) -> list[dict[str, Any]]:
    facts = collect_facts(inp, inp.as_of - timedelta(days=2), inp.as_of, ("halt",))
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in facts:
        groups.setdefault((str(f.get("ticker")), str(f.get("halt_time"))), []).append(f)
    return [g[-1] for g in groups.values() if not any(r.get("resumed_at") for r in g)]


def _macro_daily(inp: BriefInputs) -> dict[str, Any] | None:
    """The macro view as of the brief: rebuilt from `macro/history.jsonl` (the daily file is
    rewritten through the day, so its `at` is often later than the brief's as-of); the daily files
    are the fallback when there is no history."""
    rebuilt = daily_as_of(inp.paths, inp.cfg, inp.as_of)
    if rebuilt is not None:
        return rebuilt
    for back in range(0, 4):
        rec = read_json(inp.paths.day_file(inp.paths.macro, inp.day - timedelta(days=back), ".json"))
        if rec and R.known(rec):
            at = parse_iso(rec.get("at"))
            if at is not None and at <= inp.as_of:
                return rec
    return None


# --------------------------------------------------------------------------- sections


def sec_header(inp: BriefInputs) -> list[str]:
    return [f"# Scout brief: {inp.day:%A %d %B %Y} (ET trading date)", "",
            f"Written {fmt_et_bne(inp.as_of)}. Scout never trades; this brief is a record for the owner.", ""]


def sec_calendar(inp: BriefInputs) -> list[str]:
    if calendar_covers(inp.day) and calendar_covers(inp.day + timedelta(days=45)):
        return []
    return ["**Warning:** the market calendar does not cover the next 45 days. Copy Guardian's new holiday "
            "table; gate 5 answers unknown once the next session is not covered.", ""]


def sec_macro(inp: BriefInputs) -> list[str]:
    lines = ["## Macro events", ""]
    daily = _macro_daily(inp)
    events = list((daily or {}).get("events") or [])
    horizon = inp.as_of + timedelta(days=7)
    shown = []
    for ev in sorted(events, key=lambda e: str(e.get("event_at"))):
        at = parse_iso(ev.get("event_at"))
        if at is None or not (inp.as_of - timedelta(hours=12) <= at <= horizon):
            continue
        bits = [f"{_when_day(ev.get('event_at'))}: {md_escape(ev.get('kind'), 30)}"]
        for key in ("prior", "street", "nowflation_forecast", "cleveland", "kalshi"):
            if ev.get(key) is not None:
                bits.append(f"{key} {md_escape(ev.get(key), 20)}")
        names = [md_escape(n, 16) for n in ev.get("sources") or [] if isinstance(n, str)]
        if names:
            bits.append("from " + ", ".join(names))
        agree = ev.get("sources_agree")
        bits.append("sources agree" if agree is True else "sources DISAGREE" if agree is False else "one source")
        shown.append("- " + "; ".join(bits))
    if daily is None:
        lines.append("No macro data yet (nowflation, BLS and the FOMC table have not been read).")
    elif not shown:
        lines.append("No CPI, NFP, PCE or FOMC event in the next 7 days.")
    lines += shown
    lines += _lean_lines((daily or {}).get("forecast_lean") or [], inp.as_of, horizon)
    lines += ["", "Macro events are recorded only; they never block a hold in v1.", NOWFLATION_CREDIT, ""]
    return lines


def _signed(value: Any) -> str:
    return f"{value:+g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "n/a"


def _lean_lines(leans: list[Any], as_of: datetime, horizon: datetime) -> list[str]:
    """Nowflation's forecast lean per event in the next 7 days (SS 7.3: informational, untested)."""
    out = []
    for lean in sorted((x for x in leans if isinstance(x, dict)), key=lambda x: str(x.get("event_at"))):
        at = parse_iso(lean.get("event_at"))
        if at is None or not (as_of - timedelta(hours=12) <= at <= horizon):
            continue
        graded = lean.get("graded_prints")
        extra = f", vs Cleveland Fed {_signed(lean['vs_cleveland'])}" if lean.get("vs_cleveland") is not None else ""
        status = lean.get("receipt_status")
        extra += f" (receipt {md_escape(status, 20)})" if isinstance(status, str) and status else ""
        out.append(f"- {_when_day(lean.get('event_at'))}: {md_escape(lean.get('kind'), 30)} forecast vs prior "
                   f"{_signed(lean.get('vs_prior'))}, vs street {_signed(lean.get('vs_street'))}{extra}; graded past "
                   f"prints {graded if isinstance(graded, int) and not isinstance(graded, bool) else 'n/a'}")
    if not out:
        return []
    return ["", f"Nowflation forecast lean ({LEAN_NOTE}):", *out]


def sec_regime(inp: BriefInputs) -> list[str]:
    daily = _macro_daily(inp) or {}
    regime = daily.get("inflation_regime") or "unknown"
    status = daily.get("regime_status")
    vix = daily.get("vix") if isinstance(daily.get("vix"), Mapping) else None
    lines = ["## Inflation regime and VIX", "",
             f"- Inflation regime: {md_escape(regime, 20)}" + (f" ({md_escape(status, 30)})" if status else "")
             + " (from Nowflation's nowcast; a slow label)",
             f"- VIX close: {md_escape(vix.get('close'), 12)} on {md_escape(vix.get('date'), 12)}" if vix
             else "- VIX close: unknown", ""]
    return lines


def sec_catalysts(inp: BriefInputs) -> list[str]:
    facts, folded = fold_offering_filings(catalysts(inp))
    labels = _labels(inp, prior_close_before(inp.as_of))
    lines = ["## Overnight catalysts (since the prior close, newest first)", ""]
    if not facts:
        lines += ["None seen in the monitored sources (PR Newswire, Stock Titan, SEC EDGAR).", ""]
        return lines
    for f in facts[:MAX_CATALYSTS]:
        text = (f"- {_when(f.get('published_at'))} **{md_escape(f.get('ticker'), 12)}** "
                f"[{md_escape(f.get('class'), 24)}] {md_escape(f.get('title'), 160)} ({md_escape(f.get('source'), 20)})")
        more = folded.get(str(f.get("fact_id")), 0)
        if more:
            text += f" (+{more} more offering filings by this issuer)"
        lab = labels.get(str(f.get("fact_id")))
        if lab and lab.get("status") == "labelled":
            state = "verified" if lab.get("verified") else "unverified"
            text += f"; Claude label {md_escape(lab.get('catalyst_type'), 30)} ({state})"
        lines.append(text)
    if len(facts) > MAX_CATALYSTS:
        lines.append(f"- ... and {len(facts) - MAX_CATALYSTS} more in state/facts.")
    lines += ["", "Scout has no price feed: a ticker is left out only when a Radar find prices it above "
              f"${inp.cfg.scan_max_price:g}.", ""]
    return lines


def sec_halts(inp: BriefInputs) -> list[str]:
    halts = open_halts(inp)
    lines = ["## Open halts", ""]
    if not halts:
        lines.append("No open halt in the Nasdaq Trader feed (record only).")
    for h in halts:
        lines.append(f"- **{md_escape(h.get('ticker'), 12)}** halted {_when(h.get('halt_time'))}, code "
                     f"{md_escape(h.get('halt_code'), 10)}, market {md_escape(h.get('halt_market'), 12)}")
    return lines + [""]


def sec_due_windows(inp: BriefInputs) -> list[str]:
    lines = ["## Earnings due windows among recent finds", ""]
    if inp.radar_skipped:
        return lines + ["radar_dir is empty: Radar's finds are skipped.", ""]
    symbols = sorted({f.symbol for f in inp.finds})
    if not symbols:
        return lines + ["No Radar finds today or on the prior trading day.", ""]
    latest: dict[str, dict[str, Any]] = {}
    for path in R.day_files(inp.paths.edgar_due, inp.as_of - timedelta(days=3), inp.as_of):
        for rec in read_jsonl(path):
            at = parse_iso(rec.get("computed_at"))
            if R.known(rec) and at is not None and at <= inp.as_of:
                latest[str(rec.get("ticker")).upper()] = rec
    for s in symbols:
        rec = latest.get(s)
        if rec is None:
            lines.append(f"- {md_escape(s, 12)}: no due-window record yet")
            continue
        extra = []
        if rec.get("overdue"):
            extra.append("OVERDUE")
        if rec.get("nt_recent"):
            extra.append("late-filing notice")
        lines.append(f"- {md_escape(s, 12)}: {md_escape(rec.get('status'), 24)}, due "
                     f"{md_escape(rec.get('due_start'), 12)} to {md_escape(rec.get('due_end'), 12)}"
                     + (f" ({', '.join(extra)})" if extra else ""))
    return lines + [""]


def _snapshot(inp: BriefInputs) -> dict[str, Any] | None:
    best = None
    for rec in read_jsonl(inp.root / R.JASON_SNAPSHOTS):
        if R.known(rec) and R.visible(rec, inp.as_of):
            best = rec
    return best


def sec_jason(inp: BriefInputs) -> list[str]:
    lines = ["## Jason's levels", ""]
    snap = _snapshot(inp)
    today = inp.day.isoformat()
    setups = [s for s in (snap or {}).get("setups") or [] if isinstance(s, Mapping)
              and (not s.get("expires") or str(s.get("expires")) >= today)
              and (not s.get("session_date") or str(s.get("session_date")) <= today)]
    if not setups:
        return lines + ["**No current levels file.** Add Jason's latest read with Add Levels.", ""]
    frame_syms = {m.upper() for m in inp.cfg.jason_market_symbols}
    frame = [s for s in setups if str(s.get("symbol", "")).upper() in frame_syms]
    rest = [s for s in setups if s not in frame]

    def row(s: Mapping[str, Any]) -> str:
        levels = []
        for lv in s.get("levels") or []:
            if isinstance(lv, Mapping):
                readings = "/".join(md_escape(r, 12) for r in lv.get("readings") or [])
                levels.append(f"{md_escape(lv.get('kind'), 16)} {readings}" + (" (ambiguous)" if lv.get("ambiguous") else ""))
        return (f"- **{md_escape(s.get('symbol'), 12)}** {md_escape(s.get('direction'), 12)} "
                f"({md_escape(s.get('source_kind'), 10)} {md_escape(s.get('session_date'), 12)}, expires "
                f"{md_escape(s.get('expires'), 12)}): " + ("; ".join(levels) or "no levels"))

    if frame:
        lines += ["Market frame:", *[row(s) for s in frame], ""]
    lines += [row(s) for s in rest[:MAX_SETUPS]]
    if len(rest) > MAX_SETUPS:
        lines.append(f"- ... and {len(rest) - MAX_SETUPS} more setups.")
    return lines + ["", "Jason's levels are records only; they satisfy no gate.", ""]


def sec_health(inp: BriefInputs) -> list[str]:
    lines = ["## Source health", ""]
    hb = read_json(inp.paths.heartbeat_file)
    if not hb or not R.known(hb):
        return lines + ["No heartbeat yet: Scout has not run, so every answer is unknown.", ""]
    at = parse_iso(hb.get("at"))
    stale = at is None or (inp.as_of - at).total_seconds() > inp.cfg.scout_stale_s or hb.get("stopped") is not False
    lines.append(f"Heartbeat {_when_day(hb.get('at'))}" + (" (STALE or stopped: answers are unknown)" if stale else ""))
    for name, st in sorted((hb.get("sources") or {}).items()):
        if not isinstance(st, Mapping):
            continue
        gap = ", coverage gap open" if st.get("gap_open") else ""
        lines.append(f"- {md_escape(name, 24)}: {md_escape(st.get('status'), 12)}, last ok "
                     f"{_when(st.get('last_ok')) if st.get('last_ok') else 'never'}{gap}")
    return lines + [""]


def sec_llm(inp: BriefInputs) -> list[str]:
    lines = ["## Claude API", ""]
    llm = inp.llm or {}
    if not inp.cfg.llm_enabled:
        return lines + ["LLM off (llm_enabled = false): this brief is deterministic only.", ""]
    spent, month = float(llm.get("spent_today_usd") or 0.0), float(llm.get("spent_month_usd") or 0.0)
    lines.append(f"Spend today ${spent:.4f} of ${inp.cfg.llm_daily_cost_cap_usd:.2f}; this month ${month:.4f} of "
                 f"${inp.cfg.llm_monthly_cost_cap_usd:.2f}.")
    calls = llm.get("calls_today") or {}
    if calls:
        lines.append("Calls today: " + ", ".join(f"{j} {calls.get(j, 0)}" for j in sorted(calls)) +
                     f"; web searches {llm.get('searches_today', 0)} of {inp.cfg.web_search_max_per_day}.")
    if llm.get("cap_reached"):
        lines.append("**LLM cap reached:** the Claude jobs stop for the day; facts, answers and this brief carry on.")
    lines.append("Labels are evidence only and never change an answer. J1 has no price feed (DESIGN D8): it "
                 "labels every listed ticker's news within the daily call cap.")
    return lines + [""]


def sec_verify(inp: BriefInputs) -> list[str]:
    week = iso_week(inp.day)
    lines = [f"## Verification sample ({week})", ""]
    sample = None
    for rec in read_jsonl(inp.root / R.VERIFY / "samples.jsonl"):
        if R.known(rec) and rec.get("week") == week:
            sample = rec
    if sample is None:
        return lines + [f"No sample yet this week (picked on {inp.cfg.verify_sample_weekday} evenings from the "
                        "week's answers).", ""]
    symbols = ", ".join(md_escape(s, 12) for s in sample.get("symbols") or []) or "(none)"
    return lines + [f"Hand-check these with `python -m scout check`: {symbols}", ""]
