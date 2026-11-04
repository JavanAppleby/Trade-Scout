"""Scout's context and main loop (DESIGN 2.1, 8).

`python -m scout run`: lock, runtime folders, startup warnings, reader params, heartbeat
"starting", then the loop: due jobs, heartbeat, sleep. Ctrl+C or a `STOP` (or `STOP.txt`) file in
Scout's folder stops it; the heartbeat says "stopped" on exit. Scout deletes the STOP file when it
stops because of it, and a STOP file left in the folder before the start is deleted with a warning
(else every later start stopped at once until the owner found the file; third review).

`ctx.live_beat_at` is the time of the main loop's last live beat (None until the first). Jobs that
read Scout's own answers through `scout_reader` (the answers jobs) wait for it: before it, the
latest beat is the "starting" one and every answer would be stale. With the real clock a `Pulse`
thread keeps beating while one job runs long, so readers never see Scout as down because of one
slow host or Claude call. The log file moves to the new ET date's name by itself
(`scout.logs.DailyFileHandler`). Services and job modules are imported lazily, so the
scaffold runs before every unit exists (a missing module logs "not built").
"""

from __future__ import annotations

import argparse
import importlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scout import FORMAT
from scout.calendar import calendar_covers
from scout.clock import Clock, SystemClock, et_date, iso
from scout.config import ScoutConfig, load_config
from scout.heartbeat import Heartbeat, Pulse, SourceStatusBoard
from scout.paths import ScoutPaths
from scout.running import AlreadyRunning, ScoutLock
from scout.scheduler import Job, run_due
from scout.store import append_jsonl, canonical_json, read_jsonl, sha256_text

JOB_MODULES = ("scout.jobs_wire", "scout.jobs_macro", "scout.jobs_levels", "scout.jobs_discord", "scout.jobs_answers",
               "scout.jobs_llm", "scout.jobs_brief")
_PARAM_META = ("format", "first_seen_at", "config_sha256")
EXIT_ALREADY_RUNNING = 4

__all__ = ["EXIT_ALREADY_RUNNING", "JOB_MODULES", "AppContext", "build_context", "cli_run", "config_sha256",
           "load_jobs", "run",
           "startup_warnings", "write_params"]


@dataclass
class AppContext:
    cfg: ScoutConfig
    paths: ScoutPaths
    clock: Clock
    board: SourceStatusBoard
    log: logging.Logger
    _services: dict[str, Any] = field(default_factory=dict)
    live_beat_at: datetime | None = None

    def _service(self, key: str, module: str, attr: str) -> Any:
        if key not in self._services:
            self._services[key] = getattr(importlib.import_module(module), attr)(self)
        return self._services[key]

    def fetcher(self) -> Any:
        return self._service("fetcher", "scout.http", "build_fetcher")

    def raw(self) -> Any:
        if "raw" not in self._services:
            self._services["raw"] = importlib.import_module("scout.raw").RawStore(self.paths)
        return self._services["raw"]

    def facts(self) -> Any:
        if "facts" not in self._services:
            self._services["facts"] = importlib.import_module("scout.facts").FactWriter(
                self.paths, self.clock, self.cfg.fact_dedupe_days)
        return self._services["facts"]

    def health(self) -> Any:
        if "health" not in self._services:
            self._services["health"] = importlib.import_module("scout.health").HealthWriter(self.paths)
        return self._services["health"]

    def ticker_map(self) -> Any:
        return self._service("ticker_map", "scout.edgar_submissions", "build_ticker_map")

    def submissions(self) -> Any:
        return self._service("submissions", "scout.edgar_submissions", "build_submissions")

    def llm_transport(self) -> Any:
        return self._service("llm_transport", "scout.llm_client", "build_transport")


def build_context(config_path: Path | str, clock: Clock | None = None,
                  log: logging.Logger | None = None) -> AppContext:
    cfg = load_config(config_path)
    paths = ScoutPaths.from_config(cfg)
    return AppContext(cfg=cfg, paths=paths, clock=clock or SystemClock(), board=SourceStatusBoard(cfg),
                      log=log or logging.getLogger("scout"))


def load_jobs(ctx: AppContext, modules: Sequence[str] = JOB_MODULES, strict: bool = False) -> list[Job]:
    jobs: list[Job] = []
    for name in modules:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if strict or exc.name != name:
                raise
            ctx.log.info("not built yet: %s", name)
            continue
        jobs.extend(module.build_jobs(ctx))
    return jobs


def config_sha256(cfg: ScoutConfig) -> str:
    """sha256 over every config value except the contact email."""
    return sha256_text(canonical_json(cfg.public_dict()))


def write_params(ctx: AppContext) -> bool:
    """Append the reader params to `state/params.jsonl` when they differ from the last line."""
    params = ctx.cfg.reader_params()
    rows = read_jsonl(ctx.paths.params_file)
    if rows:
        last = {k: v for k, v in rows[-1].items() if k not in _PARAM_META}
        if last == params:
            return False
    record = {"format": FORMAT, "first_seen_at": iso(ctx.clock.now()), "config_sha256": config_sha256(ctx.cfg),
              **params}
    append_jsonl(ctx.paths, ctx.paths.params_file, record)
    return True


def startup_warnings(ctx: AppContext, now: datetime) -> list[str]:
    """Plain-English warnings for the log and the brief (DESIGN 8)."""
    out: list[str] = []
    today = et_date(now)
    if not (calendar_covers(today) and calendar_covers(today + timedelta(days=45))):
        out.append("The market calendar does not cover the next 45 days: copy Guardian's new holiday table.")
    if not ctx.cfg.http_allowed():
        out.append("contact_email is empty: Scout makes no web requests at all until it is set.")
    if not ctx.cfg.radar_dir.strip():
        out.append("radar_dir is empty: Radar's finds are skipped.")
    if not ctx.cfg.pilot_dir.strip():
        out.append("pilot_dir is empty: Pilot's positions are skipped.")
    try:
        has_levels = any(ctx.paths.levels.glob("*.json"))
    except OSError:
        has_levels = False
    if not has_levels:
        out.append("No Jason levels file has been added yet.")
    return out


def _stop_files(ctx: AppContext) -> list[Path]:
    out = []
    for path in ctx.paths.stop_files:
        try:
            if path.is_file():
                out.append(path)
        except OSError:
            continue
    return out


def _remove(ctx: AppContext, files: Sequence[Path]) -> None:
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            ctx.log.warning("Could not delete %s (%s): delete it before starting Scout again.", path.name,
                            type(exc).__name__)


def run(ctx: AppContext, *, max_ticks: int | None = None, jobs: Sequence[Job] | None = None,
        until: datetime | None = None, pulse: bool | None = None) -> int:
    """The main loop. Returns 0 on a normal stop, `EXIT_ALREADY_RUNNING` (4) when another Scout runs
    here (its own code, so `Start Scout.bat` can say so; 2 is the CLI's bad-input code). `max_ticks` and
    `until` (a clock time) end the loop early; tests use them, `python -m scout run` never does.
    `pulse` (default: only with the real clock) starts the heartbeat thread."""
    lock = ScoutLock(ctx.paths)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"Scout did not start: {exc}.")
        return EXIT_ALREADY_RUNNING
    heartbeat = Heartbeat(ctx.paths, ctx.cfg, ctx.board, ctx.clock)
    ctx.live_beat_at = None
    beater = Pulse(heartbeat, ctx.clock) if (isinstance(ctx.clock, SystemClock) if pulse is None else pulse) else None
    try:
        ctx.paths.ensure()
        now = ctx.clock.now()
        for warning in startup_warnings(ctx, now):
            ctx.log.warning(warning)
        leftover = _stop_files(ctx)
        if leftover:
            ctx.log.warning("A %s file was left in Scout's folder from an earlier stop; deleted it so Scout runs.",
                            leftover[0].name)
            _remove(ctx, leftover)
        write_params(ctx)
        heartbeat.starting(now)
        job_list = list(jobs) if jobs is not None else load_jobs(ctx)
        if beater is not None:
            beater.start()
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            found = _stop_files(ctx)
            if found:
                ctx.log.info("%s file found; Scout stops and deletes it", found[0].name)
                _remove(ctx, found)
                break
            now = ctx.clock.now()
            if until is not None and now >= until:
                break
            run_due(job_list, now, ctx.log, ctx.clock)
            now = ctx.clock.now()
            if heartbeat.due(now):
                heartbeat.beat(now)
            if heartbeat.last_at is not None:
                ctx.live_beat_at = heartbeat.last_at
            ticks += 1
            sleep = getattr(ctx.clock, "sleep", None)
            if sleep is not None:
                sleep(ctx.cfg.loop_interval_s)
    except KeyboardInterrupt:
        ctx.log.info("Ctrl+C: Scout stops")
    finally:
        if beater is not None:
            beater.stop()
        heartbeat.stopped(ctx.clock.now())
        ctx.live_beat_at = None
        lock.release()
    return 0


def cli_run(ctx: AppContext, args: argparse.Namespace) -> int:
    return run(ctx)
