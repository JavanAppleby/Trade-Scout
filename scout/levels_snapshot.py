"""The Jason snapshot: the merged current setups, for `scout_reader` (DESIGN D4, 5.9).

`state\\jason\\snapshots.jsonl` gets one line each time the merged current set changes (a file
added, a setup expiring), so a stdlib reader can answer "as of t" with the snapshot first seen at
or before t. Prices are decimal strings. One declared level keeps all its readings together:
`{kind, readings, ambiguous, note}`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from scout.clock import iso
from scout.levels import load_folder
from scout.models import JasonSnapshot
from scout.paths import ScoutPaths
from scout.store import append_jsonl, canonical_json
from scout_reader.records import JASON_SNAPSHOTS, known, read_jsonl

__all__ = ["build_snapshot", "latest_expiry", "last_snapshot", "snapshot_setup", "write_if_changed"]


def _levels_record(setup: Mapping) -> list[dict[str, Any]]:
    """Regroup the loader's one-level-per-reading list into declared levels (by `position`)."""
    out: list[dict[str, Any]] = []
    by_position: dict[Any, dict[str, Any]] = {}
    for lvl in setup.get("levels", ()):
        key = (lvl.get("position"), lvl["kind"])
        readings = list(lvl.get("alternatives") or ()) if lvl.get("ambiguous") else [str(lvl["price"])]
        if not readings:
            readings = [str(lvl["price"])]
        entry = by_position.get(key)
        if entry is None:
            entry = {"kind": lvl["kind"], "readings": readings, "ambiguous": bool(lvl.get("ambiguous")),
                     "note": str(lvl.get("note") or "")}
            by_position[key] = entry
            out.append(entry)
    return out


def snapshot_setup(setup: Mapping) -> dict[str, Any]:
    """One setup as the snapshot stores it (DESIGN 5.9)."""
    return {
        "symbol": setup["symbol"],
        "direction": setup["direction"],
        "attribution": setup["attribution"],
        "source_kind": setup.get("source_kind", "tuesday"),
        "session_date": setup["session_date"],
        "expires": setup["expires"],
        "horizon": setup.get("horizon", ""),
        "levels": _levels_record(setup),
    }


def build_snapshot(levels_dir: Path | str, today: date, now: datetime) -> JasonSnapshot:
    """The merged current set of `levels_dir` on `today` (ET), first seen at `now`."""
    plan = load_folder(levels_dir, today)
    return JasonSnapshot(first_seen_at=iso(now), today_et=today.isoformat(), files=tuple(plan["files"]),
                         setups=tuple(snapshot_setup(s) for s in plan["setups"]),
                         warnings=tuple(plan["warnings"]))


def latest_expiry(snapshot: JasonSnapshot) -> str | None:
    """The latest `expires` among the snapshot's setups (for the heartbeat), or None."""
    return max((str(s["expires"]) for s in snapshot.setups), default=None)


def last_snapshot(paths: ScoutPaths) -> dict[str, Any] | None:
    """The last readable snapshot line, or None."""
    records = [r for r in read_jsonl(paths.root / JASON_SNAPSHOTS) if known(r)]
    return records[-1] if records else None


def _content(rec: Mapping[str, Any]) -> str:
    return canonical_json({"files": list(rec.get("files") or ()), "setups": list(rec.get("setups") or ())})


def write_if_changed(paths: ScoutPaths, snapshot: JasonSnapshot) -> bool:
    """Append `snapshot` when its files or setups differ from the last line (or there is none)."""
    record = snapshot.to_record()
    previous = last_snapshot(paths)
    if previous is not None and _content(previous) == _content(record):
        return False
    append_jsonl(paths, paths.root / JASON_SNAPSHOTS, record)
    return True
