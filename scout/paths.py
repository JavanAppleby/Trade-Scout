"""Scout's own folder and the one gate every Scout write goes through (SCOUT_SPEC 2.4, DESIGN 4).

`ScoutPaths.writable(p)` resolves `p` (symlinks included) and refuses anything outside Scout's
folder, and anything inside Radar's or Pilot's folder (`others`). Reads of those folders happen only
in `scout.inputs` and `scout.levels_bars`, open-read-close.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from scout_reader import records as R

if TYPE_CHECKING:  # pragma: no cover
    from scout.config import ScoutConfig

__all__ = ["OwnFolderError", "ScoutPaths", "is_within", "overlaps"]


class OwnFolderError(RuntimeError):
    """A write outside Scout's own folder (or into another bot's folder) was refused."""


def _key(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def is_within(child: Path | str, parent: Path | str) -> bool:
    """True when `child` is `parent` or below it (case-insensitive on Windows). Both are compared
    as given; resolve them first when symlinks matter."""
    c, p = _key(child), _key(parent).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def overlaps(a: Path | str, b: Path | str) -> bool:
    """True when either resolved folder is the other or contains it."""
    ra, rb = Path(a).resolve(strict=False), Path(b).resolve(strict=False)
    return is_within(ra, rb) or is_within(rb, ra)


@dataclass(frozen=True)
class ScoutPaths:
    """Scout's folder layout. `levels_dir`/`inbox_dir` are relative to `root` (config keys);
    `others` are folders Scout must never write into (Radar's and Pilot's)."""

    root: Path
    levels_dir: str = "state/levels"
    inbox_dir: str = "inbox"
    others: tuple[Path, ...] = field(default=())

    def __post_init__(self) -> None:
        root = Path(self.root).resolve(strict=False)
        object.__setattr__(self, "root", root)
        others = tuple(Path(o).resolve(strict=False) for o in self.others if str(o).strip())
        for other in others:
            if overlaps(root, other):
                raise OwnFolderError(f"Scout's folder and {other} overlap; they must be separate folders")
        object.__setattr__(self, "others", others)
        for name in ("levels_dir", "inbox_dir"):
            target = (root / getattr(self, name)).resolve(strict=False)
            if not is_within(target, root):
                raise OwnFolderError(f"{name} must be inside Scout's folder")

    @classmethod
    def from_config(cls, cfg: ScoutConfig) -> ScoutPaths:
        others = tuple(cfg.scout_dir / d for d in (cfg.radar_dir, cfg.pilot_dir) if d.strip())
        return cls(root=cfg.scout_dir, levels_dir=cfg.levels_dir, inbox_dir=cfg.inbox_dir, others=others)

    # ------------------------------------------------------------------ folders

    @property
    def state(self) -> Path:
        return self.root / R.STATE

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def inbox(self) -> Path:
        return (self.root / self.inbox_dir).resolve(strict=False)

    @property
    def inbox_jason(self) -> Path:
        return self.inbox / "jason"

    @property
    def levels(self) -> Path:
        return (self.root / self.levels_dir).resolve(strict=False)

    @property
    def facts(self) -> Path:
        return self.root / R.FACTS

    @property
    def health(self) -> Path:
        return self.root / R.HEALTH

    @property
    def answers(self) -> Path:
        return self.root / R.ANSWERS

    @property
    def labels(self) -> Path:
        return self.root / R.LABELS

    @property
    def llm(self) -> Path:
        return self.root / R.LLM

    @property
    def macro(self) -> Path:
        return self.root / R.MACRO

    @property
    def raw(self) -> Path:
        return self.root / R.RAW

    @property
    def beats(self) -> Path:
        return self.root / R.BEATS

    @property
    def edgar_due(self) -> Path:
        return self.root / R.EDGAR_DUE

    @property
    def jason(self) -> Path:
        return self.root / R.JASON

    @property
    def verify(self) -> Path:
        return self.root / R.VERIFY

    # ------------------------------------------------------------------ files

    @property
    def heartbeat_file(self) -> Path:
        return self.root / R.HEARTBEAT

    @property
    def params_file(self) -> Path:
        return self.root / R.PARAMS

    @property
    def lock_file(self) -> Path:
        return self.state / "scout.lock"

    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"

    @property
    def stop_files(self) -> tuple[Path, ...]:
        """`STOP` and `STOP.txt` (Explorer's New > Text Document renamed to STOP, with extensions hidden)."""
        return (self.stop_file, self.root / "STOP.txt")

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    def day_file(self, folder: Path, day: date, ext: str = ".jsonl") -> Path:
        return Path(folder) / f"{day.isoformat()}{ext}"

    # ------------------------------------------------------------------ the gate

    def writable(self, p: Path | str) -> Path:
        """`p` resolved, when it is inside Scout's folder and in no other bot's; else OwnFolderError."""
        target = Path(p)
        if not target.is_absolute():
            target = self.root / target
        target = target.resolve(strict=False)
        if not is_within(target, self.root):
            raise OwnFolderError(f"refused a write outside Scout's folder: {target}")
        for other in self.others:
            if is_within(target, other):
                raise OwnFolderError(f"refused a write into another bot's folder: {target}")
        return target

    def ensure(self) -> None:
        """Create the runtime folders (all inside Scout's folder)."""
        for folder in (self.state, self.logs, self.reports, self.inbox_jason, self.levels, self.facts,
                       self.health, self.answers, self.labels, self.llm, self.macro, self.raw, self.beats,
                       self.edgar_due, self.jason, self.verify):
            self.writable(folder).mkdir(parents=True, exist_ok=True)
