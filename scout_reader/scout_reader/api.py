"""The public reader API (SCOUT_SPEC 10.3, DESIGN 3.1-3.2). Stdlib only; never raises.

`Reader(scout_dir)` answers six questions about one symbol at one aware instant `as_of`, from
Scout's files alone. Each answer is a Result dict: {"value": "true"|"false"|"unknown",
"evidence": [...], "stale": bool, "reasons": [...]} plus extra keys per function. The module-level
functions take `scout_dir` first and do the same.

Rules every function applies (DESIGN 3.2):
1. As-of: a record counts only when `first_seen_at <= as_of` and, where present,
   `published_at <= as_of` (`records.visible`); due records count when `computed_at <= as_of`,
   health records when `end <= as_of`, beats when `at <= as_of`.
2. Freshness: Scout is fresh only when a beat with `at` in (as_of - stale_s, as_of] exists and the
   latest such beat says `stopped: false`. `stale_s` is the Reader's argument, else the params'
   `scout_stale_s`, else 120. Stale makes every value `unknown` ("scout_stale").
3. Params: the last `params.jsonl` record with `first_seen_at <= as_of`. None, an unknown format,
   or an unreadable line after it gives `unknown` ("no_params", "unknown_format:params",
   "unreadable_line:params").
4. Format and damage: records with a missing or unknown `format` are ignored, and so are lines that
   do not parse or a last line without a newline. When any file an answer read had such a line, a
   `false` becomes `unknown` (the lost line might have been the fact that mattered); `true` stays.
5. Files read: only the ET-date files that cover the answer's window.
6. Source status: a source not `ok` in the governing beat makes the answers that depend on it
   `unknown` ("source_not_ok:<name>").
Every exception becomes `unknown` with reason "error:<ExceptionType>"; a naive `as_of` is refused
("naive_as_of"). With `now` given, an `as_of` later than `now()` is refused ("future_as_of"), since
records for it may still arrive.

`halt_open`, `jason_tags`, `market_context` and `scanner_alerts` (Paper Gains scanner cards, descriptive,
for Radar's later tags) hand the records to `scout_reader.halts`, `scout_reader.jason`,
`scout_reader.macro` and `scout_reader.scanner`, imported on first use; a missing module answers
`unknown` ("not_built:<module>"). Gate 6 asks `scout_reader.coverage` for gaps the same way.

`entry_blocks` (owner, 2026-11-04) is Radar's question before a buy signal: known bad news on file
(an offering, reverse split, listing notice or bankruptcy in the last 3 trading days, an open halt,
a CPI / FOMC / jobs release minutes away). It differs from the others in one way: evidence on file
makes it `true` even when Scout is stale (rule 2) or a source is not ok (rule 6); those only turn
`false` into `unknown`. It reads through the Reader's file cache (`scout_reader.cache`), so keep one
Reader for repeated calls; `cache=False` reads every file afresh (same answers, slower).
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scout_reader import blocks as B
from scout_reader import gates
from scout_reader import records as R
from scout_reader.cache import FileCache

__all__ = [
    "Reader", "entry_blocks", "event_before_next_session", "fresh_catalyst", "halt_open", "hold_allowed",
    "jason_tags", "market_context", "news_headline", "scanner_alerts",
]

Result = dict[str, Any]
TRI = ("true", "false", "unknown")
JASON_TAGS = ("on_jason_list", "near_jason_level", "jason_conflict", "jason_market_frame")
MARKET_FIELDS = ("macro_event_next_session", "inflation_regime", "vix", "next_macro_event")
HALT_LOOKBACK_DAYS = 30  # multi-day halts appear only under their first HaltDate
MACRO_LOOKBACK_DAYS = 45  # scheduled events are first seen days or weeks ahead
DUE_LOOKBACK_DAYS = 4  # a trading day's due records can sit in up to four ET-date files (long weekends)
DEFAULT_LOOKBACK_DAYS = 45
SCANNER_DEFAULT_DAYS = 7  # scanner_alerts looks back this many days unless told otherwise
SCANNER_MAX_DAYS = 14  # Scout refuses posts older than discord_max_post_age_days (14)

hold_allowed = gates.hold_allowed


def _error(exc: BaseException) -> Result:
    return R.result("unknown", (), False, (f"error:{type(exc).__name__}",))


def _is_aware(dt: object) -> bool:
    return isinstance(dt, datetime) and dt.tzinfo is not None and dt.utcoffset() is not None


class _Scan:
    """Tolerant reading that also remembers damage (rule 4)."""

    def __init__(self) -> None:
        self.notes: set[str] = set()

    def read(self, path: Path, label: str) -> list[dict[str, Any]]:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return []
        except OSError:
            self.notes.add(f"unreadable_file:{label}")
            return []
        out: list[dict[str, Any]] = []
        lines = data.split(b"\n")
        if lines[-1].strip():
            self.notes.add(f"partial_line:{label}")
        for raw in lines[:-1]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                self.notes.add(f"unreadable_line:{label}")
                continue
            if not isinstance(rec, dict):
                self.notes.add(f"unreadable_line:{label}")
            elif not R.known(rec):
                self.notes.add(f"unknown_format:{label}")
            else:
                out.append(rec)
        return out

    def days(self, root: Path, folder: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in R.day_files(root / folder, start, end):
            out.extend(self.read(path, f"{folder.rsplit('/', 1)[-1]}/{path.name}"))
        return out


class Reader:
    """Reads one Scout folder. Cheap to build. Every call reads the files it needs afresh, except
    `entry_blocks`, which keeps what it parsed in this Reader's cache (`cache=False`: no cache)."""

    def __init__(self, scout_dir: str | os.PathLike[str], *, now: Callable[[], datetime] | None = None,
                 stale_s: float | None = None, cache: bool = True) -> None:
        self.scout_dir = scout_dir
        self.now = now
        self.stale_s = stale_s
        self.cache: FileCache | None = FileCache() if cache else None

    # ------------------------------------------------------------------ shared context

    def _root(self) -> Path:
        return Path(self.scout_dir)

    def _refuse(self, symbol: object, as_of: object, *, need_symbol: bool = True) -> Result | None:
        if not _is_aware(as_of):
            return R.result("unknown", (), False, ("naive_as_of",))
        if need_symbol and (not isinstance(symbol, str) or not symbol.strip()):
            return R.result("unknown", (), False, ("bad_symbol",))
        if self.now is not None:
            now = self.now()
            if _is_aware(now) and as_of > now:  # type: ignore[operator]
                return R.result("unknown", (), False, ("future_as_of",))
        return None

    def _params(self, as_of: datetime) -> tuple[dict[str, Any] | None, list[str]]:
        best: dict[str, Any] | None = None
        best_at: datetime | None = None
        best_index = -1
        damaged_after: list[int] = []
        path = self._root() / R.PARAMS
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return None, ["no_params"]
        except OSError:
            return None, ["unreadable_file:params"]
        lines = data.split(b"\n")
        for index, raw in enumerate(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                if index == len(lines) - 1:
                    raise ValueError("partial last line")
                rec = json.loads(raw.decode("utf-8"))
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
            except (UnicodeDecodeError, ValueError, RecursionError):
                damaged_after.append(index)
                continue
            at = R.parse_time(rec.get("first_seen_at"))
            if at is None:
                damaged_after.append(index)
                continue
            if at <= as_of and (best_at is None or at >= best_at):
                best, best_at, best_index = rec, at, index
        if best is None:
            return None, ["no_params"]
        if not R.known(best):
            return None, ["unknown_format:params"]
        if any(i > best_index for i in damaged_after):
            return None, ["unreadable_line:params"]
        return best, []

    def _stale_s(self, params: Mapping[str, Any] | None) -> float:
        for value in (self.stale_s, (params or {}).get("scout_stale_s")):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
        return R.DEFAULT_STALE_S

    def _beat(self, as_of: datetime, stale_s: float) -> tuple[dict[str, Any] | None, list[str]]:
        """The latest beat in (as_of - stale_s, as_of]; None (stale) when there is none, it says
        stopped, or its format is unknown."""
        start = as_of - timedelta(seconds=stale_s)
        best: dict[str, Any] | None = None
        best_at: datetime | None = None
        for path in R.day_files(self._root() / R.BEATS, start, as_of):
            for rec in R.read_jsonl(path):
                at = R.parse_time(rec.get("at"))
                if at is None or not (start < at <= as_of):
                    continue
                if best_at is None or at >= best_at:
                    best, best_at = rec, at
        if best is None:
            return None, ["scout_stale"]
        if not R.known(best):
            return None, ["scout_stale", "unknown_format:beats"]
        if best.get("stopped") is not False:
            return None, ["scout_stale", "scout_stopped"]
        return best, []

    def _context(self, as_of: datetime) -> tuple[dict[str, Any] | None, dict[str, Any] | None, Result | None]:
        """(params, beat, early result). The early result is set when an answer must be unknown."""
        params, p_reasons = self._params(as_of)
        beat, b_reasons = self._beat(as_of, self._stale_s(params))
        if beat is None:
            return params, None, R.result("unknown", (), True, b_reasons + p_reasons)
        if params is None:
            return None, beat, R.result("unknown", (), False, p_reasons)
        return params, beat, None

    @staticmethod
    def _lookback(params: Mapping[str, Any], key: str, default: int) -> int:
        value = params.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 3650:
            return int(value)
        return default

    def _facts(self, scan: _Scan, as_of: datetime, days: float = 0.0,
               start: datetime | None = None) -> list[dict[str, Any]]:
        """Visible facts first seen from `start` (else `days` before as_of) to as_of."""
        begin = start if start is not None else as_of - timedelta(days=days)
        return [r for r in scan.days(self._root(), R.FACTS, begin, as_of) if R.visible(r, as_of)]

    def _health(self, scan: _Scan, start: datetime, as_of: datetime) -> list[dict[str, Any]]:
        out = []
        for rec in scan.days(self._root(), R.HEALTH, start - timedelta(days=1), as_of):
            end = R.parse_time(rec.get("end"))
            if end is not None and end <= as_of and R.parse_time(rec.get("start")) is not None:
                out.append(rec)
        return out

    @staticmethod
    def _finish(res: Result, scan: _Scan) -> Result:
        """Rule 4: damage in a file this answer read turns `false` into `unknown`."""
        if res.get("value") == "false" and scan.notes:
            res["value"] = "unknown"
            res["reasons"] = list(res.get("reasons", [])) + sorted(scan.notes)
        return res

    @staticmethod
    def _guard(fn: Callable[[], Any], fallback: Callable[[Result], Any]) -> Any:
        try:
            return fn()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - the reader never raises into the caller
            return fallback(_error(exc))

    # ------------------------------------------------------------------ gates 6 and 5

    def _gate6(self, symbol: object, as_of: datetime, headline: bool) -> Result:
        refused = self._refuse(symbol, as_of)
        if refused is not None:
            return refused
        sym = str(symbol).strip().upper()
        params, beat, early = self._context(as_of)
        if early is not None:
            return early
        scan = _Scan()
        start, _end = gates.gate6_window(as_of)
        facts = self._facts(scan, as_of, start=start)
        health = self._health(scan, start, as_of)
        fn = gates.news_headline_from if headline else gates.fresh_catalyst_from
        return self._finish(fn(facts, health, beat, params, sym, as_of), scan)

    def news_headline(self, symbol: str, as_of: datetime) -> Result:
        """Any wire or filing fact naming `symbol` since the prior close; `classes` lists them."""
        def fallback(r: Result) -> Result:
            r["classes"] = []
            return r
        return self._guard(lambda: self._gate6(symbol, as_of, True), fallback)

    def fresh_catalyst(self, symbol: str, as_of: datetime) -> Result:
        """Gate 6 (SS 7.1)."""
        return self._guard(lambda: self._gate6(symbol, as_of, False), lambda r: r)

    def _gate5(self, symbol: object, as_of: datetime) -> Result:
        refused = self._refuse(symbol, as_of)
        if refused is not None:
            return refused
        sym = str(symbol).strip().upper()
        params, beat, early = self._context(as_of)
        if early is not None:
            return early
        assert params is not None
        scan = _Scan()
        lookback = max(self._lookback(params, "earnings_lookback_days", DEFAULT_LOOKBACK_DAYS), MACRO_LOOKBACK_DAYS)
        facts = self._facts(scan, as_of, lookback)
        due = scan.days(self._root(), R.EDGAR_DUE, as_of - timedelta(days=DUE_LOOKBACK_DAYS), as_of)
        return self._finish(gates.event_before_next_session_from(facts, due, beat, params, sym, as_of), scan)

    def event_before_next_session(self, symbol: str, as_of: datetime) -> Result:
        """Gate 5 (SS 7.2)."""
        return self._guard(lambda: self._gate5(symbol, as_of), lambda r: r)

    # ------------------------------------------------------------------ delegated answers

    @staticmethod
    def _module(name: str) -> Any:
        """The sibling module, or None when it is not built."""
        try:
            return importlib.import_module(f"scout_reader.{name}")
        except ModuleNotFoundError as exc:
            if exc.name in (f"scout_reader.{name}", "scout_reader"):
                return None
            raise

    @staticmethod
    def _one(res: object, *, tri: bool) -> Result:
        """A delegated Result checked for shape; with `tri`, the value must be true/false/unknown."""
        if not isinstance(res, Mapping) or not isinstance(res.get("value"), str):
            return R.result("unknown", (), False, ("error:bad_result",))
        out = dict(res)
        if tri and out["value"] not in TRI:
            return R.result("unknown", (), False, ("error:bad_result",))
        out["evidence"] = [str(e) for e in out.get("evidence") or []] if isinstance(
            out.get("evidence", []), (list, tuple)) else []
        out["reasons"] = [str(e) for e in out.get("reasons") or []] if isinstance(
            out.get("reasons", []), (list, tuple)) else []
        out["stale"] = bool(out.get("stale", False))
        return out

    def _halt(self, symbol: object, as_of: datetime) -> Result:
        refused = self._refuse(symbol, as_of)
        if refused is not None:
            return refused
        sym = str(symbol).strip().upper()
        _params, beat, early = self._context(as_of)
        if early is not None:
            return early
        module = self._module("halts")
        if module is None:
            return R.result("unknown", (), False, ("not_built:halts",))
        scan = _Scan()
        facts = [f for f in self._facts(scan, as_of, HALT_LOOKBACK_DAYS) if f.get("kind") == "halt"]
        res = self._one(module.halt_open_from(facts, beat, sym, as_of), tri=True)
        return self._finish(res, scan)

    def halt_open(self, symbol: str, as_of: datetime) -> Result:
        """An open trading halt on `symbol` at `as_of` (record only, SS 7.5)."""
        return self._guard(lambda: self._halt(symbol, as_of), lambda r: r)

    @staticmethod
    def _all(keys: tuple[str, ...], res: Result) -> dict[str, Result]:
        return {k: dict(res, evidence=list(res["evidence"]), reasons=list(res["reasons"])) for k in keys}

    def _jason(self, symbol: object, price: float | None, as_of: datetime,
               frame_prices: Mapping[str, float] | None, near_pct: float | None) -> dict[str, Result]:
        refused = self._refuse(symbol, as_of)
        if refused is not None:
            return self._all(JASON_TAGS, refused)
        sym = str(symbol).strip().upper()
        params, _beat, early = self._context(as_of)
        if early is not None:
            return self._all(JASON_TAGS, early)
        assert params is not None
        module = self._module("jason")
        if module is None:
            return self._all(JASON_TAGS, R.result("unknown", (), False, ("not_built:jason",)))
        snapshot: dict[str, Any] | None = None
        snap_at: datetime | None = None
        scan = _Scan()  # damage or a future format turns a false tag into unknown (rule 4)
        for rec in scan.read(self._root() / R.JASON_SNAPSHOTS, "jason/snapshots.jsonl"):
            at = R.parse_time(rec.get("first_seen_at"))
            if at is not None and at <= as_of and (snap_at is None or at >= snap_at):
                snapshot, snap_at = rec, at
        pct = near_pct
        if pct is None:
            value = params.get("jason_near_level_pct")
            pct = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        markets = params.get("jason_market_symbols")
        if isinstance(markets, list) and all(isinstance(m, str) for m in markets):
            got = module.jason_tags_from(snapshot, sym, price, as_of, pct, frame_prices, market_symbols=markets)
        else:  # params written before the key existed: the reader's default frame
            got = module.jason_tags_from(snapshot, sym, price, as_of, pct, frame_prices)
        if not isinstance(got, Mapping):
            return self._all(JASON_TAGS, R.result("unknown", (), False, ("error:bad_result",)))
        out = {k: self._finish(self._one(got.get(k), tri=False), scan) for k in JASON_TAGS}
        for k, v in got.items():
            if k not in out:
                out[k] = v
        return out

    def jason_tags(self, symbol: str, price: float | None, as_of: datetime, *,
                   frame_prices: Mapping[str, float] | None = None,
                   near_pct: float | None = None) -> dict[str, Result]:
        """Jason's tags for `symbol` (descriptive only, SS 5.5): one Result per tag."""
        return self._guard(lambda: self._jason(symbol, price, as_of, frame_prices, near_pct),
                           lambda r: self._all(JASON_TAGS, r))

    def _market(self, as_of: datetime) -> dict[str, Result]:
        refused = self._refuse(None, as_of, need_symbol=False)
        if refused is not None:
            return self._all(MARKET_FIELDS, refused)
        params, beat, early = self._context(as_of)
        if early is not None:
            return self._all(MARKET_FIELDS, early)
        module = self._module("macro")
        if module is None:
            return self._all(MARKET_FIELDS, R.result("unknown", (), False, ("not_built:macro",)))
        scan = _Scan()
        history = [r for r in scan.read(self._root() / R.MACRO_HISTORY, "macro/history.jsonl")
                   if R.visible(r, as_of)]
        facts = [f for f in self._facts(scan, as_of, MACRO_LOOKBACK_DAYS) if f.get("kind") in gates.MACRO_KINDS]
        got = module.market_context_from(history, facts, params, as_of, beat=beat)  # rule 6
        if not isinstance(got, Mapping):
            return self._all(MARKET_FIELDS, R.result("unknown", (), False, ("error:bad_result",)))
        out = {k: self._one(got.get(k), tri=k == "macro_event_next_session") for k in MARKET_FIELDS}
        out["macro_event_next_session"] = self._finish(out["macro_event_next_session"], scan)
        for k, v in got.items():
            if k not in out:
                out[k] = v
        return out

    def market_context(self, as_of: datetime) -> dict[str, Result]:
        """Macro events, inflation regime and VIX at `as_of` (record only, SS 7.3, 7.6)."""
        return self._guard(lambda: self._market(as_of), lambda r: self._all(MARKET_FIELDS, r))

    def _scanner(self, symbol: object, as_of: datetime, days: object) -> Result:
        refused = self._refuse(symbol, as_of)
        if refused is not None:
            return dict(refused, alerts=[])
        sym = str(symbol).strip().upper()
        _params, beat, early = self._context(as_of)
        if early is not None:
            return dict(early, alerts=[])
        module = self._module("scanner")
        if module is None:
            return dict(R.result("unknown", (), False, ("not_built:scanner",)), alerts=[])
        span = days if isinstance(days, (int, float)) and not isinstance(days, bool) and 0 < days else             SCANNER_DEFAULT_DAYS
        start = as_of - timedelta(days=min(float(span), SCANNER_MAX_DAYS))
        scan = _Scan()
        facts = [f for f in self._facts(scan, as_of, start=start) if f.get("kind") == "scanner"]
        got = module.scanner_alerts_from(facts, beat, sym, as_of, start)
        res = self._one(got, tri=True)
        res["alerts"] = [a for a in got.get("alerts") or [] if isinstance(a, Mapping)] if isinstance(
            got, Mapping) and isinstance(got.get("alerts"), list) else []
        return self._finish(res, scan)

    def scanner_alerts(self, symbol: str, as_of: datetime, *, days: float = SCANNER_DEFAULT_DAYS) -> Result:
        """Paper Gains scanner cards naming `symbol` posted in the `days` before `as_of` (descriptive
        only, never a gate input; for Radar's later tags, SS 9.1). `alerts` lists them newest first."""
        return self._guard(lambda: self._scanner(symbol, as_of, days), lambda r: dict(r, alerts=[]))


    # ------------------------------------------------------------------ Radar's entry blocks

    def _blocks(self, symbol: object, as_of: datetime, lookback_days: object, before_min: object,
                after_min: object) -> Result:
        refused = self._refuse(symbol, as_of)
        if refused is not None:
            return dict(refused, blocks=[])
        sym = str(symbol).strip().upper()
        days = int(lookback_days) if _number(lookback_days) and 0 < lookback_days < 60 else B.DEFAULT_LOOKBACK_DAYS  # type: ignore[operator]
        before = float(before_min) if _number(before_min) and 0 <= before_min <= 1440 else B.DEFAULT_MACRO_BEFORE_MIN  # type: ignore[operator]
        after = float(after_min) if _number(after_min) and 0 <= after_min <= 1440 else B.DEFAULT_MACRO_AFTER_MIN  # type: ignore[operator]
        cache = self.cache if self.cache is not None else FileCache()
        params, p_reasons = self._params(as_of)
        beat, b_reasons = B.find_beat(self._root(), cache, as_of, self._stale_s(params))
        return B.entry_blocks_in(self._root(), cache, params, p_reasons, beat, b_reasons, sym, as_of, days, before,
                                 after)

    def entry_blocks(self, symbol: str, as_of: datetime, *, lookback_days: int = B.DEFAULT_LOOKBACK_DAYS,
                     macro_before_min: float = B.DEFAULT_MACRO_BEFORE_MIN,
                     macro_after_min: float = B.DEFAULT_MACRO_AFTER_MIN) -> Result:
        """Any known bad news that blocks a buy signal for `symbol` at `as_of` (block only; owner,
        2026-11-04; SS 7.8). `true` when a block is on file, even with Scout stale; `false` only when
        Scout is fresh and every source it needs is ok and covered; `unknown` otherwise. `blocks`
        lists {kind, class, fact_id, first_seen_at, title}, kinds as `scout_reader.blocks.BLOCK_KINDS`."""
        return self._guard(lambda: self._blocks(symbol, as_of, lookback_days, macro_before_min, macro_after_min),
                           lambda r: dict(r, blocks=[]))


# --------------------------------------------------------------------------- module-level conveniences


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value  # NaN is not


def _safe(fn: Callable[[], Any], fallback: Callable[[Result], Any]) -> Any:
    try:
        return fn()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        return fallback(_error(exc))


def _same(r: Result) -> Result:
    return r


def news_headline(scout_dir: str | os.PathLike[str], symbol: str, as_of: datetime) -> Result:
    return _safe(lambda: Reader(scout_dir).news_headline(symbol, as_of), lambda r: dict(r, classes=[]))


def fresh_catalyst(scout_dir: str | os.PathLike[str], symbol: str, as_of: datetime) -> Result:
    return _safe(lambda: Reader(scout_dir).fresh_catalyst(symbol, as_of), _same)


def event_before_next_session(scout_dir: str | os.PathLike[str], symbol: str, as_of: datetime) -> Result:
    return _safe(lambda: Reader(scout_dir).event_before_next_session(symbol, as_of), _same)


def halt_open(scout_dir: str | os.PathLike[str], symbol: str, as_of: datetime) -> Result:
    return _safe(lambda: Reader(scout_dir).halt_open(symbol, as_of), _same)


def jason_tags(scout_dir: str | os.PathLike[str], symbol: str, price: float | None, as_of: datetime,
               **kw: Any) -> dict[str, Result]:
    return _safe(lambda: Reader(scout_dir).jason_tags(symbol, price, as_of, **kw),
                 lambda r: Reader._all(JASON_TAGS, r))


def market_context(scout_dir: str | os.PathLike[str], as_of: datetime) -> dict[str, Result]:
    return _safe(lambda: Reader(scout_dir).market_context(as_of), lambda r: Reader._all(MARKET_FIELDS, r))


def scanner_alerts(scout_dir: str | os.PathLike[str], symbol: str, as_of: datetime, **kw: Any) -> Result:
    return _safe(lambda: Reader(scout_dir).scanner_alerts(symbol, as_of, **kw), lambda r: dict(r, alerts=[]))


def entry_blocks(scout_dir: str | os.PathLike[str] | Reader, symbol: str, as_of: datetime, **kw: Any) -> Result:
    """`Reader.entry_blocks`; pass a Reader (not a folder) for repeated calls, so its cache is kept."""
    return _safe(lambda: (scout_dir if isinstance(scout_dir, Reader) else Reader(scout_dir)).entry_blocks(
        symbol, as_of, **kw), lambda r: dict(r, blocks=[]))
