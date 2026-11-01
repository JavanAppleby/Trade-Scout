"""The `jason_snapshot` job (DESIGN 2.4, 8): at start, daily at 00:05 ET, and within
`CHECK_INTERVAL_S` of a change in the levels folder. It writes the snapshot when the merged set
changed and publishes the current files and latest expiry on the heartbeat (`board.set_levels`)."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from scout.clock import et_date, et_datetime
from scout.levels import level_files
from scout.levels_snapshot import build_snapshot, latest_expiry, write_if_changed

__all__ = ["CHECK_INTERVAL_S", "DAILY_AT_ET", "JasonSnapshotJob", "build_jobs", "folder_signature"]

#: DESIGN 8: daily at 00:05 ET (so a setup that expired yesterday leaves the set) and every five
#: minutes when the folder changed. Fixed by the design; not config keys.
DAILY_AT_ET = time(0, 5)
CHECK_INTERVAL_S = 300.0


def folder_signature(levels_dir: Any) -> tuple:
    """Names, sizes and modification times of the level files: changes when a file is added."""
    out = []
    for p in level_files(levels_dir):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_size, st.st_mtime_ns))
    return tuple(out)


class JasonSnapshotJob:
    name = "jason_snapshot"

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.last_day: date | None = None
        self.last_check: datetime | None = None
        self.signature: tuple | None = None

    def due(self, now: datetime) -> bool:
        if self.last_day is None:
            return True
        today = et_date(now)
        if today != self.last_day and now >= et_datetime(today, DAILY_AT_ET):
            return True
        if self.last_check is None or (now - self.last_check).total_seconds() >= CHECK_INTERVAL_S:
            self.last_check = now
            return folder_signature(self.ctx.paths.levels) != self.signature
        return False

    def run(self, now: datetime) -> None:
        self.last_day, self.last_check = et_date(now), now
        self.signature = folder_signature(self.ctx.paths.levels)
        snapshot = build_snapshot(self.ctx.paths.levels, et_date(now), now)
        if write_if_changed(self.ctx.paths, snapshot):
            self.ctx.log.info("jason snapshot: %d current setup(s) from %d file(s)", len(snapshot.setups),
                              len(snapshot.files))
            for warning in snapshot.warnings:
                self.ctx.log.info("jason levels warning: %s", warning)
        if not snapshot.files:
            self.ctx.log.info("no current Jason levels file in %s", self.ctx.paths.levels)
        self.ctx.board.set_levels(list(snapshot.files), latest_expiry(snapshot))


def build_jobs(ctx: Any) -> list[Any]:
    return [JasonSnapshotJob(ctx)]
