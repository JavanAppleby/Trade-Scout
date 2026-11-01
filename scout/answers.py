"""Scout's own answers job (SCOUT_SPEC 10.2 answers, 11; DESIGN 2.5 and 5.5).

At each `answer_times_et` (19:30 and 19:45 ET) Scout refreshes the EDGAR due record of every
covered symbol (Pilot's open positions, today's Radar finds and `watch_symbols`, `scout.inputs`)
and writes one answer per symbol to `state/answers/<ET date of as_of>.jsonl`, with `as_of` = now.
After `radar_answers_after_et` (20:00 ET) it answers today's Radar finds again with `as_of` =
`radar_answers_as_of_et` (19:45 ET). On early-close days every time is 3 hours earlier.

The answers come from `scout_reader` reading Scout's own folder, exactly as Radar and Pilot read
it (DESIGN D3), so a stored answer can be replayed. Due records are written before the answers so
the reader can see them (`computed_at <= as_of`). A due record refreshed after the Radar run's
`as_of` is not visible to it (no look-ahead): a find with no due record from the 19:30 or 19:45 run
answers gate 5 `unknown`. Nothing here changes an answer for a label (SS 2 rule 7).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from scout_reader.api import Reader

from scout.calendar import is_early_close
from scout.clock import Clock, et_date, et_datetime, iso
from scout.config import ScoutConfig
from scout.due_window import due_window
from scout.inputs import pilot_positions, radar_finds
from scout.models import AnswerRecord, DueWindowRecord, PeriodicInfo
from scout.paths import ScoutPaths
from scout.secrets import redact
from scout.store import append_jsonl

__all__ = ["EARLY_CLOSE_SHIFT_H", "GATES", "AnswersJob", "answer_from_results"]

EARLY_CLOSE_SHIFT_H = 3.0
GATES = ("fresh_catalyst", "event_before_next_session", "macro_event_next_session", "news_headline", "halt_open")

log = logging.getLogger("scout.answers")


def _value(res: object) -> str:
    value = res.get("value") if isinstance(res, Mapping) else None
    return value if value in ("true", "false", "unknown") else "unknown"


def _strings(res: object, key: str) -> list[str]:
    got = res.get(key) if isinstance(res, Mapping) else None
    return [str(x) for x in got] if isinstance(got, (list, tuple)) else []


def _stale(res: object) -> bool:
    return isinstance(res, Mapping) and res.get("stale") is True


def answer_from_results(symbol: str, as_of: datetime, written_at: datetime, origin: str,
                        results: Mapping[str, object]) -> AnswerRecord:
    """One AnswerRecord from the reader's Results, keyed by GATES."""
    return AnswerRecord(
        symbol=symbol, as_of=iso(as_of), written_at=iso(written_at), origin=origin,
        fresh_catalyst=_value(results.get("fresh_catalyst")),
        event_before_next_session=_value(results.get("event_before_next_session")),
        macro_event_next_session=_value(results.get("macro_event_next_session")),
        news_headline=_value(results.get("news_headline")), halt_open=_value(results.get("halt_open")),
        reasons={g: _strings(results.get(g), "reasons") for g in GATES},
        evidence={g: _strings(results.get(g), "evidence") for g in GATES},
        stale=any(_stale(results.get(g)) for g in GATES))


class AnswersJob:
    """Refreshes due records and writes answers. `submissions` implements
    interfaces.SubmissionsSource (FakeSubmissions in tests)."""

    def __init__(self, cfg: ScoutConfig, paths: ScoutPaths, submissions: Any, clock: Clock,
                 logger: logging.Logger | None = None) -> None:
        self.cfg, self.paths, self.submissions, self.clock = cfg, paths, submissions, clock
        self.log = logger or log

    # ------------------------------------------------------------------ times

    def _shift(self, day: date) -> timedelta:
        return timedelta(hours=EARLY_CLOSE_SHIFT_H) if is_early_close(day) else timedelta(0)

    def radar_as_of(self, day: date) -> datetime:
        """`radar_answers_as_of_et` on ET date `day` (3 hours earlier on an early close)."""
        return et_datetime(day, self.cfg.radar_answers_as_of_et) - self._shift(day)

    def answer_times(self, day: date) -> list[datetime]:
        return [et_datetime(day, t) - self._shift(day) for t in self.cfg.answer_times_et]

    def radar_after(self, day: date) -> datetime:
        return et_datetime(day, self.cfg.radar_answers_after_et) - self._shift(day)

    # ------------------------------------------------------------------ due records

    def _periodic(self, symbol: str, now: datetime) -> PeriodicInfo:
        try:
            info = self.submissions.periodic_info(symbol, now)
            if isinstance(info, PeriodicInfo):
                return info
            self.log.warning("answers: submissions gave no PeriodicInfo for %s", symbol)
        except Exception as exc:  # noqa: BLE001 - the contract says never raises; fail closed if it does
            self.log.warning("answers: submissions failed for %s: %s: %s", symbol, type(exc).__name__,
                             redact(str(exc), self.cfg))
        return PeriodicInfo(ticker=symbol, cik=None, status="fetch_failed")

    def refresh_due(self, symbols: Iterable[str], now: datetime) -> list[DueWindowRecord]:
        """One due record per symbol, appended to `state/edgar/due/<ET date of now>.jsonl`."""
        out: list[DueWindowRecord] = []
        target = self.paths.day_file(self.paths.edgar_due, et_date(now))
        for symbol in sorted({s.strip().upper() for s in symbols if s and s.strip()}):
            try:
                rec = due_window(self._periodic(symbol, now), self.cfg, now)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("answers: due window failed for %s: %s", symbol, type(exc).__name__)
                rec = due_window(PeriodicInfo(ticker=symbol, cik=None, status="fetch_failed"), self.cfg, now)
            append_jsonl(self.paths, target, rec.to_record())
            out.append(rec)
        return out

    # ------------------------------------------------------------------ answers

    def answer_symbols(self, symbols: Sequence[str], as_of: datetime, now: datetime, *,
                       origins: Mapping[str, str] | None = None, refresh: bool = True) -> list[AnswerRecord]:
        """Answers for `symbols` at `as_of` (<= now). With `refresh`, due records are refreshed at
        `now` first (visible only when now <= as_of)."""
        wanted = sorted({s.strip().upper() for s in symbols if s and s.strip()})
        if refresh:
            self.refresh_due(wanted, now)
        reader = Reader(self.paths.root, now=lambda: now)
        context = reader.market_context(as_of)
        macro = context.get("macro_event_next_session") if isinstance(context, Mapping) else None
        out: list[AnswerRecord] = []
        for symbol in wanted:
            results = {
                "fresh_catalyst": reader.fresh_catalyst(symbol, as_of),
                "event_before_next_session": reader.event_before_next_session(symbol, as_of),
                "macro_event_next_session": macro,
                "news_headline": reader.news_headline(symbol, as_of),
                "halt_open": reader.halt_open(symbol, as_of),
            }
            origin = (origins or {}).get(symbol, "manual")
            out.append(answer_from_results(symbol, as_of, now, origin, results))
        return out

    def write(self, records: Iterable[AnswerRecord]) -> int:
        """Append each answer to `state/answers/<ET date of as_of>.jsonl`."""
        count = 0
        for rec in records:
            day = et_date(datetime.fromisoformat(rec.as_of.replace("Z", "+00:00")))
            append_jsonl(self.paths, self.paths.day_file(self.paths.answers, day), rec.to_record())
            count += 1
        return count

    def covered(self, now: datetime) -> dict[str, str]:
        """symbol -> origin for the 19:30/19:45 runs: Pilot first, then Radar, then watch."""
        origins: dict[str, str] = {}
        for s in self.cfg.watch_symbols:
            origins[s.upper()] = "watch"
        for find in radar_finds(self.cfg, et_date(now)):
            origins[find.symbol] = "radar"
        for s in pilot_positions(self.cfg, now):
            origins[s] = "pilot"
        return origins

    def run_pilot(self, now: datetime) -> list[AnswerRecord]:
        """The 19:30 and 19:45 ET run: every covered symbol, as_of = now."""
        origins = self.covered(now)
        records = self.answer_symbols(list(origins), now, now, origins=origins)
        self.write(records)
        self.log.info("answers: %d covered symbol(s) answered", len(records))
        return records

    def run_radar(self, now: datetime) -> list[AnswerRecord]:
        """After 20:00 ET: today's Radar finds with as_of 19:45 ET (16:45 on an early close)."""
        day = et_date(now)
        as_of = min(self.radar_as_of(day), now)
        symbols = sorted({f.symbol for f in radar_finds(self.cfg, day)})
        records = self.answer_symbols(symbols, as_of, now, origins={s: "radar" for s in symbols})
        self.write(records)
        self.log.info("answers: %d Radar find(s) answered as of %s", len(records), iso(as_of))
        return records
