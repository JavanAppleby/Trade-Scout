"""The thesis readers at work (READS_TEST.md 5.1; owner, 2026-11-10 and 2026-11-11).

`ReadsService.tick(now)` runs in Scout's loop every `reads_poll_s` and stays quick: it picks up the
owner's notes (`scout.reads_owner`) and reads the new lines of Radar's packs file (on the first tick
after a start, also every earlier file named in the cursor that was not read to its end). Each pack
becomes one task per enabled reader, on that reader's own queue. Each reader has its own pool of
`reads_workers_per_reader` worker threads, so a slow reader (R2) never makes another (R1, the main
test) wait, a burst of finds is read in parallel, and a slow or failing Claude API never makes
Scout's loop late or delays another job. With `threaded=False`, as in most tests, `tick` works the
queues itself, pack by pack.

Order and safety under threads:
- a worker takes the oldest task of its reader whose candidate is not being read by that reader at
  the moment, so reads of one reader and candidate never overlap and run in Radar's order (a reread
  always sees its first read);
- one first read per reader and candidate; a pack a reader has a line for is never read again;
- the theses and the record of handled packs are kept under one lock;
- the cap check and the reservation of a call are one step across all threads
  (`scout.reads_budget`), so calls checked at once never cross a cap together;
- the position in Radar's file is saved only up to the start of the earliest pack not yet finished
  (and never past a pack whose line could not be written, after `APPEND_TRIES` tries), so a restart
  polls the unfinished packs again; the reads file says which packs were handled, so none is read
  twice. If that file cannot be read, the day is not marked loaded and the next pack tries again;
- a worker that died is started again on the next tick; workers are daemon threads, so nothing
  holds up Scout's stop.

For each pack and each enabled reader:
- a **first pack** (`seq` 0) is read when the reader's sample selects the candidate: a hash of
  `reads_sample_seed`, the trading day, the candidate id and the reader id, read as a number in
  [0, 1), is below `sample`. It does not depend on the time of day, so the choice is repeatable;
- a **refresh pack** is read again when the reader has `rereads` on and its thesis on the candidate
  is alive: from a first read with direction long or short and strength at least
  `reads_reread_min_strength`, until a reread says faded or invalidated, gives strength 2 or less,
  or changes direction; or `horizon_min` minutes have passed since the first read; or
  `reads_reread_max` rereads are done; or the session the first read was made in has ended (a pack
  from another session);
- a selected pack that is not read gets a skip line: `stale` (made more than
  `reads_max_pack_age_s` before its turn), `cap` (a reads cap), `refusal`, `invalid` (a cut-off or
  an answer that fails the checks) or `error` (an API failure, no key, an unreadable or unwritable
  ledger).
Packs outside `reads_sessions`, and packs a reader's sample did not select, get no line.

A read changes no answer, gate or `entry_blocks` result; nothing that answers reads these lines.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from scout_reader.api import Reader
from scout_reader.gates import trading_day_of

from scout.clock import Clock, et_date, parse_iso
from scout.config import ScoutConfig
from scout.config_reads import ReaderConfig
from scout.llm_client import execute
from scout.paths import ScoutPaths
from scout.reads_budget import CAP_REASONS, UNREADABLE, ReadsBudget, reader_job
from scout.reads_context import scout_context
from scout.reads_file import ReadsWriter, check_answer, read_line, reads_on, skip_line
from scout.reads_market import latest_record, shown_market
from scout.reads_prompt import build_read
from scout.reads_tail import PackTailer, Polled

APPEND_TRIES = 5  # a read or skip line that cannot be appended is tried this often ...
APPEND_PAUSE_S = 0.2  # ... this many seconds apart

__all__ = ["APPEND_PAUSE_S", "APPEND_TRIES", "ReadsService", "Thesis", "sample_value", "selected"]


def sample_value(seed: str, trading_day: str, candidate_id: Any, reader_id: str) -> float:
    """A number in [0, 1) from the hash of the four values (READS_TEST.md 5.1)."""
    digest = hashlib.sha256(f"{seed}|{trading_day}|{candidate_id}|{reader_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64


def selected(cfg: ScoutConfig, reader: ReaderConfig, trading_day: str, candidate_id: Any) -> bool:
    return sample_value(cfg.reads.sample_seed, trading_day, candidate_id, reader.reader_id) < reader.sample


@dataclass
class Thesis:
    first: dict[str, Any]
    ends_at: datetime
    rereads: int = 0
    alive: bool = True
    found: dict[str, Any] | None = None  # the first pack's `find`, for the reread prompt (this run only)


@dataclass(eq=False)
class _Entry:
    """One polled pack: where it lies in Radar's file and the readers that still have to handle it."""

    polled: Polled
    open: set[str] = field(default_factory=set)
    failed: bool = False


@dataclass(eq=False)
class _Task:
    entry: _Entry
    reader: ReaderConfig

    @property
    def key(self) -> tuple[str, str, str]:
        p = self.entry.polled.pack
        return (self.reader.reader_id, str(p.get("trading_day")), str(p.get("candidate_id")))


def _starts_thesis(cfg: ScoutConfig, rec: Mapping[str, Any]) -> bool:
    strength = rec.get("strength")
    return (rec.get("direction") in ("long", "short") and isinstance(strength, int)
            and strength >= cfg.reads.reread_min_strength)


def _ends_thesis(first: Mapping[str, Any], rec: Mapping[str, Any]) -> bool:
    strength = rec.get("strength")
    return (rec.get("status") in ("faded", "invalidated") or (isinstance(strength, int) and strength <= 2)
            or rec.get("direction") != first.get("direction"))


class ReadsService:
    def __init__(self, cfg: ScoutConfig, paths: ScoutPaths, clock: Clock, transport: Any, *,
                 log: logging.Logger | None = None, threaded: bool = True) -> None:
        self.cfg, self.paths, self.clock, self.transport = cfg, paths, clock, transport
        self.log = log or logging.getLogger("scout.reads")
        self.tailer = PackTailer(cfg, paths, self.log)
        self.writer = ReadsWriter(paths, clock)
        self.budget = ReadsBudget(paths, cfg, clock)
        self.reader_api = Reader(paths.root)
        self.threaded = threaded
        self._cond = threading.Condition()  # guards the queues, entries, busy keys and the ends below
        self._queues: dict[str, list[_Task]] = {}
        self._entries: list[_Entry] = []
        self._queued_ids: set[str] = set()  # pack ids with an entry not yet finished
        self._busy: set[tuple[str, str, str]] = set()  # (reader, day, candidate) being read now
        self._running = 0
        self._ends: dict[str, int] = {}  # how far each file was polled
        self._workers: dict[str, list[threading.Thread | None]] = {}
        self._commit_lock = threading.Lock()
        self._saved = self.tailer.position()
        self._started = False
        self._state = threading.RLock()  # guards done, theses, firsts and the loaded days
        self.done: set[tuple[str, str]] = set()  # (reader id, pack id) handled
        self.firsts: set[tuple[str, str, str]] = set()  # (reader id, day, candidate id) with a first line
        self.theses: dict[tuple[str, str, str], Thesis] = {}  # (day, reader id, candidate id)
        self._days_loaded: set[str] = set()

    # ------------------------------------------------------------------ the loop side

    def tick(self, now: datetime) -> None:
        from scout.reads_owner import pick_up  # noqa: PLC0415 - keeps the owner's path importable alone

        pick_up(self.cfg, self.paths, self.writer, now, log=self.log)
        readers = self.cfg.reads.enabled_readers()
        if self.transport is None or not readers:
            return
        days = {et_date(now), trading_day_of(now)}
        if not self._started:  # an earlier day's file left short at a stop gets its lines too
            days.update(self.tailer.cursor_days())
            self._started = True
        polled = self.tailer.poll_spans(sorted(days), save=False)
        with self._cond:
            for item in polled:
                self._enqueue(item, readers)
            self._ends.update(self.tailer.position())
            self._cond.notify_all()
        if self.threaded:
            self._ensure_workers(readers)
        else:
            self.drain()
        self._commit()

    def _enqueue(self, item: Polled, readers: tuple[ReaderConfig, ...]) -> None:
        pack, pid = item.pack, str(item.pack.get("pack_id"))
        if pid in self._queued_ids:  # already waiting (a file read again from its start)
            return
        entry = _Entry(item)
        day = str(pack.get("trading_day") or "")
        if len(day) == 10 and pack.get("session") in self.cfg.reads.sessions:
            for reader in readers:
                entry.open.add(reader.reader_id)
                self._queues.setdefault(reader.reader_id, []).append(_Task(entry, reader))
        if entry.open:
            self._queued_ids.add(pid)
        self._entries.append(entry)

    def _ensure_workers(self, readers: tuple[ReaderConfig, ...]) -> None:
        """Start each reader's pool, and start again any worker that died."""
        n = self.cfg.reads.workers_per_reader
        for reader in readers:
            pool = self._workers.setdefault(reader.reader_id, [None] * n)
            for i, worker in enumerate(pool):
                if worker is None or not worker.is_alive():
                    if worker is not None:
                        self.log.warning("reads: worker %s-%d had stopped; started again", reader.reader_id, i)
                    pool[i] = threading.Thread(target=self._work, args=(reader.reader_id,),
                                               name=f"scout-reads-{reader.reader_id}-{i}", daemon=True)
                    pool[i].start()

    def idle(self) -> bool:
        with self._cond:
            return self._running == 0 and not any(self._queues.values())

    # ------------------------------------------------------------------ the worker side

    def _take(self, reader_id: str) -> _Task | None:
        """The oldest task of the reader whose candidate is not being read now (under `_cond`)."""
        queue = self._queues.get(reader_id) or []
        for i, task in enumerate(queue):
            if task.key not in self._busy:
                del queue[i]
                self._busy.add(task.key)
                self._running += 1
                return task
        return None

    def _work(self, reader_id: str) -> None:
        while True:
            with self._cond:
                task = self._take(reader_id)
                while task is None:
                    self._cond.wait()
                    task = self._take(reader_id)
            self._run(task)

    def drain(self) -> int:
        """Work every queue on the calling thread, pack by pack; returns the tasks handled."""
        handled = 0
        while True:
            with self._cond:
                tasks = [t for t in (self._take(rid) for rid in list(self._queues)) if t is not None]
            if not tasks:
                return handled
            for task in sorted(tasks, key=lambda t: self._entries.index(t.entry)):
                self._run(task)
                handled += 1

    def _run(self, task: _Task) -> None:
        ok = False
        try:
            self._handle(task.reader, task.entry.polled.pack)
            ok = True
        except Exception as exc:  # noqa: BLE001 - one bad pack never stops the reads
            self.log.error("reads: pack %s not handled for %s (%s); it is polled again at the next start",
                           str(task.entry.polled.pack.get("pack_id"))[:60], task.reader.reader_id,
                           type(exc).__name__)
        finally:
            with self._cond:
                self._busy.discard(task.key)
                self._running -= 1
                task.entry.open.discard(task.reader.reader_id)
                task.entry.failed = task.entry.failed or not ok
                if not task.entry.open:
                    self._queued_ids.discard(str(task.entry.polled.pack.get("pack_id")))
                self._cond.notify_all()
            self._commit()

    def _commit(self) -> None:
        """Save the position up to the start of the earliest unfinished pack of each file."""
        with self._commit_lock:
            with self._cond:
                pos = dict(self._ends)
                keep: list[_Entry] = []
                for e in self._entries:
                    if e.open or e.failed:
                        name = e.polled.name
                        pos[name] = min(pos.get(name, e.polled.start), e.polled.start)
                        keep.append(e)
                self._entries = keep
            if pos != self._saved:
                self.tailer.commit(pos)
                self._saved = pos

    # ------------------------------------------------------------------ one pack, one reader

    def _load_day(self, day: str) -> None:
        """Rebuild the handled packs and the theses from the day's reads file, once. A file that
        cannot be read raises, and the day stays unloaded, so the next pack tries again."""
        with self._state:
            if day in self._days_loaded:
                return
            lines = reads_on(self.paths, day, strict=True)
            for rec in lines:
                rid, pid = rec.get("reader"), rec.get("pack_id")
                if isinstance(rid, str) and isinstance(pid, str):
                    self.done.add((rid, pid))
                if rec.get("kind") == "first" or (rec.get("kind") == "skip" and rec.get("for") == "first"):
                    self.firsts.add((str(rid), day, str(rec.get("candidate_id"))))
                if rec.get("kind") == "first" and _starts_thesis(self.cfg, rec):
                    self._open(day, rec)
                elif rec.get("kind") == "reread":
                    self._after_reread(day, rec)
            self._days_loaded.add(day)

    def _open(self, day: str, rec: Mapping[str, Any], found: Any = None) -> None:
        seen = parse_iso(rec.get("first_seen_at"))
        horizon = rec.get("horizon_min") if isinstance(rec.get("horizon_min"), int) else 0
        if seen is None:
            return
        key = (day, str(rec.get("reader")), str(rec.get("candidate_id")))
        self.theses[key] = Thesis(first=dict(rec), ends_at=seen + timedelta(minutes=horizon),
                                  found=dict(found) if isinstance(found, Mapping) else None)

    def _after_reread(self, day: str, rec: Mapping[str, Any]) -> None:
        th = self.theses.get((day, str(rec.get("reader")), str(rec.get("candidate_id"))))
        if th is None:
            return
        th.rereads += 1
        if _ends_thesis(th.first, rec) or th.rereads >= self.cfg.reads.reread_max:
            th.alive = False

    def _alive(self, day: str, reader: ReaderConfig, pack: Mapping[str, Any], now: datetime) -> Thesis | None:
        with self._state:
            th = self.theses.get((day, reader.reader_id, str(pack.get("candidate_id"))))
            if th is None or not th.alive:
                return None
            ended = pack.get("session") != th.first.get("session")  # the session of the first read is over
            if now >= th.ends_at or th.rereads >= self.cfg.reads.reread_max or ended:
                th.alive = False
                return None
            return th

    def handle(self, pack: Mapping[str, Any]) -> None:
        """Every enabled reader on one pack, on the calling thread."""
        if pack.get("session") not in self.cfg.reads.sessions:
            return
        for reader in self.cfg.reads.enabled_readers():
            self._handle(reader, pack)

    def _handle(self, reader: ReaderConfig, pack: Mapping[str, Any]) -> None:
        day = str(pack.get("trading_day") or "")
        if len(day) != 10:
            return
        self._load_day(day)
        rid, cand = reader.reader_id, str(pack.get("candidate_id"))
        with self._state:
            if (rid, str(pack.get("pack_id"))) in self.done:
                return
            first_done = (rid, day, cand) in self.firsts
        now = self.clock.now()
        if pack.get("seq") == 0:
            if not first_done and selected(self.cfg, reader, day, pack.get("candidate_id")):
                self._read(reader, pack, None, now)
        elif reader.rereads:
            th = self._alive(day, reader, pack, now)
            if th is not None:
                self._read(reader, pack, th, now)

    def _append(self, rec: dict[str, Any], *, number: bool) -> dict[str, Any]:
        """The writer's append, tried `APPEND_TRIES` times (a file held open by another program, a
        virus scan); the last failure is raised, and the pack is then not committed past."""
        for attempt in range(1, APPEND_TRIES + 1):
            try:
                return self.writer.append(rec, number=number)
            except OSError as exc:
                if attempt == APPEND_TRIES:
                    raise
                self.log.warning("reads: line for %s not written (%s); trying again",
                                 str(rec.get("pack_id"))[:60], type(exc).__name__)
                time.sleep(APPEND_PAUSE_S)
        raise AssertionError("unreachable")  # pragma: no cover

    def _mark(self, reader: ReaderConfig, pack: Mapping[str, Any], kind: str) -> None:
        with self._state:
            self.done.add((reader.reader_id, str(pack.get("pack_id"))))
            if kind == "first":
                self.firsts.add((reader.reader_id, str(pack.get("trading_day")), str(pack.get("candidate_id"))))

    def _skip(self, reader: ReaderConfig, pack: Mapping[str, Any], why: str, kind: str, *, detail: str = "",
              cost: float = 0.0) -> None:
        self._append(skip_line(reader, pack, why, for_kind=kind, detail=detail, cost_usd=cost), number=False)
        self._mark(reader, pack, kind)

    def _market(self, pack: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
        """The market block this read is shown (0.3.2): Scout's market feed (when it is on) or the
        pack's own block, whichever is newer, each only within `reads_market_max_age_s`."""
        r = self.cfg.reads
        rec = latest_record(self.paths, now) if r.market_enabled else None
        return shown_market(pack.get("market"), rec, now, r.market_max_age_s, r.market_symbols)

    def _read(self, reader: ReaderConfig, pack: Mapping[str, Any], th: Thesis | None, now: datetime) -> None:
        kind = "reread" if th is not None else "first"
        made = parse_iso(pack.get("made_at"))
        if made is None or (now - made).total_seconds() > self.cfg.reads.max_pack_age_s:
            self._skip(reader, pack, "stale", kind)
            return
        job = reader_job(reader.reader_id)
        stop = self.budget.stop_reason(job)
        if stop is not None:
            self._skip(reader, pack, "error" if stop == UNREADABLE else "cap", kind, detail=stop)
            return
        try:
            ctx = scout_context(self.cfg, self.paths, str(pack.get("symbol")), now, self.reader_api)
            market = self._market(pack, now)
            request = build_read(pack, ctx, reader, self.cfg, first=th.first if th is not None else None, now=now,
                                 found=th.found if th is not None else None, market=market)
        except ValueError as exc:
            self._skip(reader, pack, "error", kind, detail=f"prompt:{type(exc).__name__}")
            return
        outcome = execute(self.transport, request, self.budget, self.budget.paths, self.clock)
        if outcome.status == "refused":
            why = "cap" if outcome.reason in CAP_REASONS else "error"
            self._skip(reader, pack, why, kind, detail=outcome.reason)
            return
        if not outcome.ok:
            why = {"refusal": "refusal", "api_error": "error"}.get(outcome.validation, "invalid")
            detail = "cut_off" if outcome.validation == "max_tokens" else outcome.validation or outcome.reason
            self._skip(reader, pack, why, kind, detail=detail, cost=outcome.cost_usd)
            return
        q = pack.get("quote") if isinstance(pack.get("quote"), Mapping) else {}
        answer, problem = check_answer(outcome.obj, reread=th is not None, ref_bid=q.get("bid"), ref_ask=q.get("ask"))
        if answer is None:
            self._skip(reader, pack, "invalid", kind, detail=problem, cost=outcome.cost_usd)
            return
        line = read_line(kind, reader, pack, answer, read_id="", facts_used=list(request.fact_ids),
                         parent_read_id=th.first.get("read_id") if th is not None else None,
                         usage=outcome.usage, cost_usd=outcome.cost_usd, market_shown=market)
        written = self._append(line, number=True)
        self._mark(reader, pack, kind)
        day = str(pack.get("trading_day"))
        with self._state:
            if th is None:
                if _starts_thesis(self.cfg, written):
                    self._open(day, written, pack.get("find"))
            else:
                self._after_reread(day, written)
