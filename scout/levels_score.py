"""The Jason levels scorer: report only, never a gate (SCOUT_SPEC 5.4, DESIGN 2.4).

Provenance: one scorer built from `ai-trading-platform` `research/jason_scorecard.py` (sha256
5b84f291207f4c7e4be773832ad6765507c40129085fc75753e4c9543f0ec2c3) and
`research/levels_scorecard.py` (sha256
e55ced087b700f3fe0ba20c8580e4c92b94ab030d60b05e54cde8e32b274aa94), commit
d62a282659f4c70fba7450471a2add98eef5af82, with the four SCOUT_SPEC 5.4 fixes:

1. Levels are read **through the loader** (`scout.levels.load_plan`), so ranges, pairs and
   `price_alternatives` expand into one scored reading each (and say they are ambiguous), and an
   ASR artefact such as "33874" is never scored unless a human wrote it as an alternative.
2. Each setup's window is its own: the sessions after `session_date` up to its `expires`; a
   `horizon` naming weeks, months or a quarter gets at least `levels_long_horizon_sessions` sessions.
   Only setups whose window has closed before `today` are scored (no half-finished windows).
3. The uniform-random null is seeded with `hashlib.sha256`, not `hash()`, so it is identical in
   every process.
4. Kept: held / lost / reached / expired with "lost dominates" (support mirrored for resistance;
   the side comes from the close at the read, as before); the trigger race between the first
   target and the invalidation, where a bar touching both counts as invalidated; tolerance
   max(`levels_tolerance_atr_mult` x ATR14, `levels_tolerance_floor_pct` x price); n reported
   everywhere and any rate on fewer than `levels_score_min_scored` scored levels printed
   INDICATIVE. A rate cannot be formatted without its null (`NullRequired`).

Point in time: a read is scored only against bars strictly after its session date; its reference
close and ATR come from bars on or before it. Bars come from an injected `BarsLoader`
(`scout.levels_bars`). Nothing here writes except `cli_levels_score` (one report file).
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from scout.calendar import next_trading_day
from scout.levels import JasonPlanError, level_files, load_plan
from scout.levels_bars import BarsLoader, CsvBars, DailyBar, RadarBars

__all__ = [
    "HORIZON_LONG_SESSIONS", "OUTCOMES", "SCORED_KINDS", "TRIGGER_OUTCOMES", "LevelScore", "NullRequired",
    "Scorecard", "TriggerScore", "atr", "cli_levels_score", "format_rate", "null_for", "null_seed", "render",
    "score_folder", "score_level", "tolerance_for", "trigger_outcome", "window_end",
]

#: A setup whose `horizon` names weeks, months or a quarter is given at least this many sessions
#: (the old scorer's weekly horizon). Not in config.toml yet (reported as a gap).
HORIZON_LONG_SESSIONS = 10
_LONG_HORIZON = re.compile(r"\b(weeks|months?|quarters?)\b", re.IGNORECASE)
ATR_PERIOD = 14
#: How far back the loader is asked for history (the null's window is levels_null_window_bars).
_HISTORY_CALENDAR_DAYS_PER_BAR = Decimal("1.5")

SCORED_KINDS = ("support", "resistance", "invalidation", "re-entry")
OUTCOMES = ("held", "lost", "reached", "expired")
TRIGGER_OUTCOMES = ("target", "invalidated", "neither", "no_trigger")


class NullRequired(ValueError):
    """A rate was asked for without its null. A rate with no chance rate beside it is noise."""


# --------------------------------------------------------------------------- tolerance and windows


def atr(bars: Sequence[DailyBar], n: int = ATR_PERIOD) -> Decimal | None:
    """Mean true range of the last `n` bars (needs n + 1 bars), or None."""
    if n <= 0 or len(bars) < n + 1:
        return None
    recent = bars[-(n + 1):]
    ranges = []
    for prev, bar in zip(recent, recent[1:]):
        ranges.append(max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close)))
    return sum(ranges, Decimal(0)) / Decimal(len(ranges))


def tolerance_for(bars: Sequence[DailyBar], price: Decimal, cfg: Any) -> Decimal:
    """max(atr_mult x ATR14, floor_pct x price); the floor alone when ATR cannot be computed."""
    floor = price * Decimal(str(cfg.levels_tolerance_floor_pct))
    measured = atr(bars)
    if measured is None:
        return floor
    return max(floor, Decimal(str(cfg.levels_tolerance_atr_mult)) * measured)


def window_end(setup: Mapping, long_sessions: int = HORIZON_LONG_SESSIONS) -> date:
    """The last session date a setup's levels are scored over (its expires, or at least
    `long_sessions` sessions for a weeks/months/quarter horizon; `levels_long_horizon_sessions`)."""
    session = date.fromisoformat(str(setup["session_date"]))
    end = date.fromisoformat(str(setup["expires"]))
    if _LONG_HORIZON.search(str(setup.get("horizon") or "")):
        day = session
        for _ in range(int(long_sessions)):
            day = next_trading_day(day)
        end = max(end, day)
    return end


# --------------------------------------------------------------------------- resolution


def score_level(price: Decimal, side: str, forward: Sequence[DailyBar], tol: Decimal) -> str:
    """One level reading against the bars after the read: held / lost / reached / expired.

    1. a close through the level by more than `tol` is `lost` (checked first on every bar, so a
       level that held on Tuesday and broke on Thursday is lost);
    2. a touch that closed back on its side, with no close through on the next session, is `held`;
    3. a touch that did neither is `reached`; 4. no touch at all is `expired`."""
    support = side == "support"
    reached = held = False
    for i, bar in enumerate(forward):
        if (bar.close < price - tol) if support else (bar.close > price + tol):
            return "lost"
        touched = bar.low <= price if support else bar.high >= price
        if touched and not reached:
            reached = True
            closed_back = bar.close >= price if support else bar.close <= price
            nxt = forward[i + 1] if i + 1 < len(forward) else None
            next_through = nxt is not None and ((nxt.close < price - tol) if support else (nxt.close > price + tol))
            held = closed_back and not next_through
    if held:
        return "held"
    return "reached" if reached else "expired"


def trigger_outcome(trigger: Decimal, direction: str, targets: Sequence[Decimal],
                    invalidations: Sequence[Decimal], forward: Sequence[DailyBar]) -> tuple[str, str]:
    """The race after a trigger is crossed: first target versus invalidation, bar by bar.

    A bar that touches both counts as invalidated (the tie-break never flatters the source). The
    bar that crosses the trigger does not also count for the race."""
    up = direction != "short"
    target = next((t for t in sorted(targets, reverse=not up) if (t > trigger if up else t < trigger)), None)
    stop = (max(invalidations) if up else min(invalidations)) if invalidations else None
    crossed = False
    verdict = "no_trigger"
    for bar in forward:
        if not crossed:
            if (bar.high >= trigger) if up else (bar.low <= trigger):
                crossed, verdict = True, "neither"
            continue
        if stop is not None and ((bar.low <= stop) if up else (bar.high >= stop)):
            verdict = "invalidated"
            break
        if target is not None and ((bar.high >= target) if up else (bar.low <= target)):
            verdict = "target"
            break
    detail = f"target {target}" if target is not None else "the read named no target beyond this trigger"
    return verdict, detail


def null_seed(seed: str, symbol: str, session_date: str, kind: str, price: Decimal | str) -> int:
    """A per-level seed that is the same in every process (sha256, never `hash()`)."""
    digest = hashlib.sha256(f"{seed}|{symbol}|{session_date}|{kind}|{price}".encode()).hexdigest()
    return int(digest[:16], 16)


def null_for(*, seed: str, symbol: str, session_date: str, kind: str, price: Decimal, reference: Decimal,
             history: Sequence[DailyBar], forward: Sequence[DailyBar], cfg: Any) -> tuple[int, int]:
    """(held, scored) for `levels_null_draws` uniform-random levels from the same name's range over
    the last `levels_null_window_bars` bars, each given the same side rule, tolerance and bars."""
    window = list(history)[-int(cfg.levels_null_window_bars):]
    draws = int(cfg.levels_null_draws)
    if not window or draws <= 0:
        return 0, 0
    lo, hi = min(b.low for b in window), max(b.high for b in window)
    if hi <= lo:
        return 0, 0
    rng = random.Random(null_seed(seed, symbol, session_date, kind, price))
    held = scored = 0
    for _ in range(draws):
        draw = Decimal(repr(rng.uniform(float(lo), float(hi))))
        if draw <= 0:
            continue
        side = "support" if reference >= draw else "resistance"
        outcome = score_level(draw, side, forward, tolerance_for(history, draw, cfg))
        if outcome in ("held", "lost"):
            scored += 1
            held += outcome == "held"
    return held, scored


# --------------------------------------------------------------------------- the card


@dataclass
class LevelScore:
    file: str
    session_date: str
    source_kind: str
    symbol: str
    kind: str
    price: Decimal
    ambiguous: bool
    attribution: str
    side: str
    sessions: int
    tolerance: Decimal
    outcome: str
    null_held: int = 0
    null_scored: int = 0


@dataclass
class TriggerScore:
    file: str
    session_date: str
    source_kind: str
    symbol: str
    price: Decimal
    ambiguous: bool
    attribution: str
    direction: str
    sessions: int
    outcome: str
    detail: str


@dataclass
class Scorecard:
    today: str
    min_scored: int
    bars_source: str = ""
    files: int = 0
    setups: int = 0
    open_windows: int = 0
    levels: list[LevelScore] = field(default_factory=list)
    triggers: list[TriggerScore] = field(default_factory=list)
    symbols_scored: set[str] = field(default_factory=set)
    symbols_missing: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def tally(self, kind: str | None = None) -> dict[str, int]:
        rows = [r for r in self.levels if kind is None or r.kind == kind]
        return {o: sum(1 for r in rows if r.outcome == o) for o in OUTCOMES}

    def held_rate(self, kind: str | None = None) -> tuple[int, int, int, int]:
        """(held, held + lost, null held, null scored). reached and expired are not asked."""
        rows = [r for r in self.levels if kind is None or r.kind == kind]
        held = sum(1 for r in rows if r.outcome == "held")
        tested = held + sum(1 for r in rows if r.outcome == "lost")
        return held, tested, sum(r.null_held for r in rows), sum(r.null_scored for r in rows)

    def trigger_tally(self) -> dict[str, int]:
        return {o: sum(1 for r in self.triggers if r.outcome == o) for o in TRIGGER_OUTCOMES}


def format_rate(label: str, held: int, scored: int, null_held: int | None, null_scored: int | None, *,
                min_scored: int) -> str:
    """The only way a held rate is printed, and it needs its null (NullRequired otherwise)."""
    if null_held is None or null_scored is None:
        raise NullRequired(f"{label}: a rate may not be formatted without its null")
    if scored <= 0:
        return f"{label}: n/a (nothing scored, n=0)"
    rate = 100.0 * held / scored
    small = "" if scored >= min_scored else f" INDICATIVE (n={scored} < {min_scored})"
    if null_scored <= 0:
        return f"{label}: {rate:.0f}% ({held}/{scored}, n={scored}), null not computed - treat as no result{small}"
    null = 100.0 * null_held / null_scored
    lift = f"{rate / null:.2f}x" if null > 0 else "n/a"
    return f"{label}: {rate:.0f}% ({held}/{scored}, n={scored}) vs null {null:.0f}% (n={null_scored}), lift {lift}{small}"


def _plain_rate(label: str, hit: int, total: int, *, min_scored: int) -> str:
    if total <= 0:
        return f"{label}: n/a (nothing scored, n=0)"
    small = "" if total >= min_scored else f" INDICATIVE (n={total} < {min_scored})"
    return f"{label}: {100.0 * hit / total:.0f}% ({hit}/{total}, n={total}){small}"


# --------------------------------------------------------------------------- scoring a folder


def _history_start(session: date, cfg: Any) -> date:
    bars = int(cfg.levels_null_window_bars) + ATR_PERIOD + 5
    return session - timedelta(days=int(Decimal(bars) * _HISTORY_CALENDAR_DAYS_PER_BAR) + 7)


def score_folder(levels_dir: Path | str, loader: BarsLoader, today: date, cfg: Any, *,
                 since: date | None = None) -> Scorecard:
    """Score every level file in `levels_dir` (current or expired) whose windows closed before
    `today`. Files that raise are named in the notes and skipped."""
    card = Scorecard(today=today.isoformat(), min_scored=int(cfg.levels_score_min_scored),
                     bars_source=str(getattr(loader, "name", type(loader).__name__)))
    plans: list[tuple[str, dict]] = []
    for path in level_files(levels_dir):
        try:
            plan = load_plan(path)
        except JasonPlanError as exc:
            card.notes.append(f"{path.name} refused by the loader: {exc}")
            continue
        if since is not None and plan["session_date"] < since.isoformat():
            continue
        plans.append((path.name, plan))
    card.files = len(plans)

    cache: dict[str, list[DailyBar]] = {}
    for name, plan in plans:
        session_date = plan["session_date"]
        session = date.fromisoformat(session_date)
        for setup in plan["setups"]:
            card.setups += 1
            end = window_end(setup, cfg.levels_long_horizon_sessions)
            if end >= today or session >= today:
                card.open_windows += 1
                continue
            symbol = setup["symbol"]
            if symbol not in cache:
                cache[symbol] = list(loader.daily(symbol, _history_start(session, cfg), today))
            bars = [b for b in cache[symbol] if b.day <= today]
            history = [b for b in bars if b.day <= session]
            forward = [b for b in bars if session < b.day <= end]
            if not bars or not forward:
                card.symbols_missing.add(symbol)
                continue
            card.symbols_scored.add(symbol)
            _score_setup(card, name, plan, setup, history, forward, cfg)
    _add_notes(card, loader)
    return card


def _score_setup(card: Scorecard, name: str, plan: Mapping, setup: Mapping, history: list[DailyBar],
                 forward: list[DailyBar], cfg: Any) -> None:
    session_date, symbol = plan["session_date"], setup["symbol"]
    levels = setup["levels"]
    reference = history[-1].close if history else None
    for level in levels:
        kind = level["kind"]
        if kind not in SCORED_KINDS or reference is None:
            continue
        price: Decimal = level["price"]
        side = "support" if reference >= price else "resistance"
        tol = tolerance_for(history, price, cfg)
        null_held, null_scored = null_for(seed=cfg.levels_null_seed, symbol=symbol, session_date=session_date,
                                          kind=kind, price=price, reference=reference, history=history,
                                          forward=forward, cfg=cfg)
        card.levels.append(LevelScore(
            file=name, session_date=session_date, source_kind=plan["source_kind"], symbol=symbol, kind=kind,
            price=price, ambiguous=bool(level["ambiguous"]), attribution=setup["attribution"], side=side,
            sessions=len(forward), tolerance=tol, outcome=score_level(price, side, forward, tol),
            null_held=null_held, null_scored=null_scored))
    targets = [lvl["price"] for lvl in levels if lvl["kind"] == "target"]
    stops = [lvl["price"] for lvl in levels if lvl["kind"] == "invalidation"]
    for level in levels:
        if level["kind"] != "trigger":
            continue
        verdict, detail = trigger_outcome(level["price"], setup["direction"], targets, stops, forward)
        card.triggers.append(TriggerScore(
            file=name, session_date=session_date, source_kind=plan["source_kind"], symbol=symbol,
            price=level["price"], ambiguous=bool(level["ambiguous"]), attribution=setup["attribution"],
            direction=setup["direction"], sessions=len(forward), outcome=verdict, detail=detail))
    if reference is None and any(lvl["kind"] in SCORED_KINDS for lvl in levels):
        card.notes.append(f"{symbol} {session_date}: no bar on or before the read, so its levels have no side")


def _add_notes(card: Scorecard, loader: Any) -> None:
    for note in getattr(loader, "notes", ()) or ():
        if note not in card.notes:
            card.notes.append(note)
    if card.symbols_missing:
        names = sorted(card.symbols_missing)
        card.notes.append(f"no daily bars after the read for {len(names)} symbol(s): {', '.join(names[:15])}"
                          + (" ..." if len(names) > 15 else ""))
    ambiguous = sum(1 for r in card.levels if r.ambiguous) + sum(1 for r in card.triggers if r.ambiguous)
    if ambiguous:
        card.notes.append(f"{ambiguous} scored reading(s) come from ambiguous levels; every reading is scored "
                          f"separately")
    others = sum(1 for r in card.levels if r.attribution != "jason")
    if others:
        card.notes.append(f"{others} scored level reading(s) are attributed to someone other than Jason")


# --------------------------------------------------------------------------- the report


def render(card: Scorecard) -> str:
    """The Markdown report (ASCII only). n is printed beside every rate."""
    m = card.min_scored
    out = [f"# Jason levels score, {card.today}", "",
           "Report only. Nothing here is a gate: no answer, tag or order reads this file.", "",
           f"- Bars: {card.bars_source}",
           f"- Level files: {card.files}; setups: {card.setups}; windows still open (not scored): "
           f"{card.open_windows}",
           f"- Scored: {len(card.levels)} level reading(s), {len(card.triggers)} trigger reading(s); "
           f"symbols with bars {len(card.symbols_scored)}, without {len(card.symbols_missing)}",
           f"- Rates on fewer than {m} scored levels are INDICATIVE.", ""]
    if not card.files:
        out += ["Nothing to score: the levels folder holds no level file.", ""]
    elif not card.levels and not card.triggers:
        out += ["Nothing to score yet: no read has a closed window with daily bars after it.", ""]
    out += ["## Levels: held / lost / reached / expired", "",
            "Held rate = held / (held + lost). Reached and expired were never asked the question and are "
            "not in the rate. Lost dominates.", "",
            "| kind | n | held | lost | reached | expired |", "|---|---|---|---|---|---|"]
    for kind in (*SCORED_KINDS, None):
        t = card.tally(kind)
        n = sum(t.values())
        if kind is not None and n == 0:
            continue
        out.append(f"| {kind or 'all'} | {n} | {t['held']} | {t['lost']} | {t['reached']} | {t['expired']} |")
    out.append("")
    for kind in (None, *SCORED_KINDS):
        if kind is not None and not any(r.kind == kind for r in card.levels):
            continue
        out.append("- " + format_rate(f"{kind or 'all levels'} HELD", *card.held_rate(kind), min_scored=m))
    tt = card.trigger_tally()
    asked = tt["target"] + tt["invalidated"] + tt["neither"]
    out += ["", "## Triggers: first target before invalidation", "",
            "A bar touching both the target and the invalidation counts as invalidated.", "",
            "- " + _plain_rate("trigger -> first target", tt["target"], asked, min_scored=m),
            f"- invalidated first {tt['invalidated']}, neither {tt['neither']}, never triggered "
            f"{tt['no_trigger']} (n={len(card.triggers)})", ""]
    if card.levels or card.triggers:
        out += ["## Every scored reading", "", "| session | kind | symbol | level | side | sessions | outcome |",
                "|---|---|---|---|---|---|---|"]
        for r in sorted(card.levels, key=lambda r: (r.session_date, r.symbol, r.kind, r.price)):
            amb = " (ambiguous)" if r.ambiguous else ""
            out.append(f"| {r.session_date} {r.source_kind} | {r.kind} | {r.symbol} | {r.price}{amb} | {r.side} | "
                       f"{r.sessions} | {r.outcome} (null {r.null_held}/{r.null_scored}) |")
        for t in sorted(card.triggers, key=lambda r: (r.session_date, r.symbol, r.price)):
            amb = " (ambiguous)" if t.ambiguous else ""
            out.append(f"| {t.session_date} {t.source_kind} | trigger | {t.symbol} | {t.price}{amb} | "
                       f"{t.direction} | {t.sessions} | {t.outcome} ({t.detail}) |")
        out.append("")
    if card.notes:
        out += ["## Notes", ""] + [f"- {n}" for n in card.notes] + [""]
    text = "\n".join(out)
    return text.encode("ascii", "replace").decode("ascii")


def cli_levels_score(ctx: Any, args: Any) -> int:
    """`levels score [--bars radar|csv] [--csv-dir PATH] [--since DATE]`: writes
    `reports\\levels-score-<ET date>.md`. 2 on bad input."""
    from scout.clock import et_date  # noqa: PLC0415
    from scout.store import atomic_write_text  # noqa: PLC0415

    cfg = ctx.cfg
    since = None
    if getattr(args, "since", None):
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(f"--since must be YYYY-MM-DD, not {args.since!r}")
            return 2
    loader: Any
    if getattr(args, "bars", "radar") == "csv":
        if not getattr(args, "csv_dir", None):
            print("--bars csv needs --csv-dir PATH (a folder of <SYMBOL>.csv files)")
            return 2
        loader = CsvBars(args.csv_dir)
    else:
        if not cfg.radar_dir.strip():
            print("radar_dir is empty in config.toml; set it, or use --bars csv --csv-dir PATH")
            return 2
        loader = RadarBars(Path(cfg.scout_dir) / cfg.radar_dir)
    problem = loader.check()
    if problem:
        print(f"Bars: {problem}")
    now = ctx.clock.now()
    today = et_date(now)
    card = score_folder(ctx.paths.levels, loader, today, cfg, since=since)
    if problem and problem not in card.notes:
        card.notes.insert(0, problem)
    target = ctx.paths.reports / f"levels-score-{today.isoformat()}.md"
    atomic_write_text(ctx.paths, target, render(card))
    print(f"Wrote {target}")
    print(format_rate("all levels HELD", *card.held_rate(), min_scored=card.min_scored))
    return 0
