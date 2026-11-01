"""Scout's heartbeat (SCOUT_SPEC 10.1, DESIGN 2.1 and 5.3).

Every `heartbeat_interval_s` (30) the main loop calls `Heartbeat.beat(now)`, which
- rewrites `state/scout.heartbeat` atomically (temp file + os.replace, retried; `scout.store`), and
- appends one `Beat` line to `state/beats/<ET date>.jsonl`, so `scout_reader` can tell whether
  Scout was alive at any past instant (DESIGN D4).

`starting(now)` (before the loop) and `stopped(now)` (on exit) write `stopped: true` with no
sources, and append a stopped beat, so no reader accepts a crashed or finished run's last beat.
Nothing here raises: a failed write is logged once per failure streak and retried next beat.

`Pulse` keeps the beat going while one job runs long (a host that accepts connections but does not
answer, a slow Claude call): a daemon thread writes a live beat whenever one is due, but only after
the main loop has written its first live beat (so a beat never claims sources Scout has not polled
yet). Writes are serialised by a lock. The main loop starts it with the real clock only.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from scout import __version__
from scout.calendar import calendar_covers
from scout.clock import Clock, et_date, iso
from scout.config import ScoutConfig
from scout.models import Beat, HeartbeatBody, SourceState
from scout.paths import ScoutPaths
from scout.store import append_jsonl, atomic_write_json

log = logging.getLogger("scout.heartbeat")

__all__ = ["Heartbeat", "Pulse", "SourceStatusBoard", "build_body"]


class SourceStatusBoard:
    """In-memory state the heartbeat publishes (implements interfaces.StatusBoard)."""

    def __init__(self, cfg: ScoutConfig) -> None:
        self.sources: dict[str, SourceState] = {
            s.name: SourceState(enabled=s.enabled, status="starting" if s.enabled else "disabled")
            for s in cfg.sources.values()
        }
        self.llm: dict[str, Any] = {"enabled": cfg.llm_enabled, "spent_today_usd": 0.0, "cap_reached": False}
        self.levels: dict[str, Any] = {"current_files": [], "expires": None}

    def update(self, source: str, state: SourceState) -> None:
        self.sources[source] = state

    def set_llm(self, enabled: bool, spent_today_usd: float, cap_reached: bool) -> None:
        self.llm = {"enabled": bool(enabled), "spent_today_usd": round(float(spent_today_usd), 6),
                    "cap_reached": bool(cap_reached)}

    def set_levels(self, current_files: list[str], expires: str | None) -> None:
        self.levels = {"current_files": sorted(current_files), "expires": expires}


def build_body(now: datetime, board: SourceStatusBoard, *, stopped: bool = False) -> HeartbeatBody:
    """The heartbeat body (pure). A stopped body carries no sources."""
    today = et_date(now)
    covers = calendar_covers(today) and calendar_covers(today + timedelta(days=45))
    sources = {} if stopped else {n: s.to_record() for n, s in sorted(board.sources.items())}
    return HeartbeatBody(at=iso(now), version=__version__, stopped=stopped, sources=sources, llm=dict(board.llm),
                         levels=dict(board.levels), calendar_covers_next_45d=covers)


class Heartbeat:
    """Writes the heartbeat and the beats log. Never raises."""

    def __init__(self, paths: ScoutPaths, cfg: ScoutConfig, board: SourceStatusBoard, clock: Clock) -> None:
        self.paths = paths
        self.cfg = cfg
        self.board = board
        self.clock = clock
        self.last_at: datetime | None = None
        self._failing = False
        self._lock = threading.Lock()

    def due(self, now: datetime) -> bool:
        if self.last_at is None:
            return True
        elapsed = (now - self.last_at).total_seconds()
        return elapsed < 0 or elapsed >= self.cfg.heartbeat_interval_s - 1e-6

    def beat(self, now: datetime) -> bool:
        return self._write(now, stopped=False)

    def starting(self, now: datetime) -> bool:
        return self._write(now, stopped=True)

    def stopped(self, now: datetime) -> bool:
        return self._write(now, stopped=True)

    def _write(self, now: datetime, *, stopped: bool) -> bool:
        with self._lock:
            return self._write_locked(now, stopped=stopped)

    def _write_locked(self, now: datetime, *, stopped: bool) -> bool:
        try:
            body = build_body(now, self.board, stopped=stopped)
            beat = Beat(at=body.at, stopped=stopped,
                        sources={} if stopped else {n: s.status for n, s in sorted(self.board.sources.items())},
                        gap_open={} if stopped else {n: s.gap_open for n, s in sorted(self.board.sources.items())})
            atomic_write_json(self.paths, self.paths.heartbeat_file, body.to_record())
            append_jsonl(self.paths, self.paths.day_file(self.paths.beats, et_date(now)), beat.to_record())
        except Exception as exc:  # noqa: BLE001 - the heartbeat never breaks the loop
            if not self._failing:
                log.warning("heartbeat write failed (%s); tried again next beat", type(exc).__name__)
            self._failing = True
            return False
        if self._failing:
            log.info("heartbeat written again")
        self._failing = False
        if not stopped:  # a "starting" marker never delays the first live beat
            self.last_at = now
        return True


class Pulse:
    """A daemon thread that beats while the main loop is busy in a long job (see the module doc)."""

    def __init__(self, heartbeat: Heartbeat, clock: Clock, *, check_s: float | None = None) -> None:
        self.heartbeat, self.clock = heartbeat, clock
        self.check_s = float(check_s if check_s is not None else max(1.0, heartbeat.cfg.heartbeat_interval_s / 3))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> bool:
        """One check: a live beat when the loop has beaten before and one is due. Never raises."""
        try:
            if self.heartbeat.last_at is None:
                return False
            now = self.clock.now()
            if self.heartbeat.due(now):
                return self.heartbeat.beat(now)
        except Exception as exc:  # noqa: BLE001 - the pulse never breaks anything
            log.warning("heartbeat pulse failed (%s)", type(exc).__name__)
        return False

    def _loop(self) -> None:
        while not self._stop.wait(self.check_s):
            self.tick()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="scout-heartbeat", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
