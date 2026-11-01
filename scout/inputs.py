"""Read-only views of the other bots' files and the owner's lists (SCOUT_SPEC 2.4, DESIGN 2.1).

Every read is open-read-close: never a lock, never a write. A missing folder or file, or one that
cannot be read, is simply absent. Scout reads nothing of Guardian's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from scout_reader.records import read_jsonl

from scout.clock import et_date
from scout.config import ScoutConfig

__all__ = ["RadarFind", "covered_symbols", "pilot_positions", "radar_finds"]


@dataclass(frozen=True)
class RadarFind:
    candidate_id: str
    symbol: str
    found_at: str
    price: float | None


def _folder(cfg: ScoutConfig, value: str) -> Path | None:
    if not value.strip():
        return None
    return (cfg.scout_dir / value).resolve(strict=False)


def radar_finds(cfg: ScoutConfig, day: date) -> list[RadarFind]:
    """Radar's finds export for ET date `day` (`radar_dir/state/finds/<date>.jsonl`)."""
    folder = _folder(cfg, cfg.radar_dir)
    if folder is None:
        return []
    out: list[RadarFind] = []
    for rec in read_jsonl(folder / "state" / "finds" / f"{day.isoformat()}.jsonl"):
        symbol = rec.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        price = rec.get("price")
        try:
            price_f = float(price) if price is not None and not isinstance(price, bool) else None
        except (TypeError, ValueError):
            price_f = None
        out.append(RadarFind(candidate_id=str(rec.get("candidate_id", "")), symbol=symbol.strip().upper(),
                             found_at=str(rec.get("found_at", "")), price=price_f))
    return out


def pilot_positions(cfg: ScoutConfig, now: datetime) -> list[str]:
    """Open positions from Pilot's plan folder (`pilot_dir/state/plans/*.json`, keys `symbol` and
    `status`). Provisional until Pilot ships; an unreadable file counts as absent."""
    folder = _folder(cfg, cfg.pilot_dir)
    if folder is None:
        return []
    plans = folder / "state" / "plans"
    try:
        files = sorted(plans.glob("*.json"))
    except OSError:
        return []
    out: set[str] = set()
    for path in files:
        try:
            with open(path, "rb") as fh:
                rec = json.loads(fh.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        symbol, status = rec.get("symbol"), rec.get("status")
        if isinstance(symbol, str) and symbol.strip() and str(status).lower() == "open":
            out.add(symbol.strip().upper())
    return sorted(out)


def covered_symbols(cfg: ScoutConfig, now: datetime) -> list[str]:
    """Pilot positions, today's Radar finds and `watch_symbols`: upper-cased, sorted, unique."""
    symbols = set(pilot_positions(cfg, now))
    symbols.update(f.symbol for f in radar_finds(cfg, et_date(now)))
    symbols.update(s.upper() for s in cfg.watch_symbols)
    return sorted(symbols)
