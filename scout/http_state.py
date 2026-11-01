"""The fetcher's per-host limits, kept across restarts (SCOUT_SPEC 4.1 #7, 12; DESIGN 2.2).

`HttpFetcher` enforces a daily cap per host (nowflation.com: at most 10 requests an ET day), a
minimum spacing per host (www.nasdaqtrader.com: 60 s) and a backoff after a 403 or 429. Held only in
memory, every restart reset them: three restarts on a CPI day could send 13 requests to
nowflation.com, and a restart within 60 s of the last halt poll sent the next one at once. This file
keeps, per host, the ET day and its request count, the last request time and any backoff, plus the
last request time per source, in `state/http_hosts.json` (written atomically after each change).

An unreadable or foreign file is ignored with a warning (the limits then start empty, as before);
it never stops Scout. The file holds no header, URL, User-Agent or contact email.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from scout.clock import iso, parse_iso

__all__ = ["FILE_NAME", "HARD_MAX_REQUESTS_PER_DAY", "HostLimits", "HostStateFile", "cap_key", "hard_cap"]

FILE_NAME = "http_hosts.json"
FORMAT = 1
# Hard daily caps in code, whatever the config says (SS 4.1 #7, SS 12 "enforced per host in code").
# A cap covers the host and every host under it (www.nowflation.com shares nowflation.com's count).
HARD_MAX_REQUESTS_PER_DAY: dict[str, int] = {"nowflation.com": 10}


def cap_key(host: str) -> str:
    """The name a host's daily count is kept under: the hard-capped domain it belongs to, else itself."""
    h = (host or "").strip().lower().rstrip(".")
    for domain in HARD_MAX_REQUESTS_PER_DAY:
        if h == domain or h.endswith("." + domain):
            return domain
    return h


def hard_cap(host: str) -> int | None:
    """The hard daily cap for `host` (or its domain), or None."""
    return HARD_MAX_REQUESTS_PER_DAY.get(cap_key(host))


@dataclass
class HostLimits:
    """What the fetcher remembers about the hosts and sources it has asked."""

    day_count: dict[str, tuple[date, int]] = field(default_factory=dict)  # cap key -> (ET day, requests)
    last_request: dict[str, datetime] = field(default_factory=dict)  # host -> last send time
    backoff_s: dict[str, float] = field(default_factory=dict)
    blocked_until: dict[str, datetime] = field(default_factory=dict)
    source_last_request: dict[str, datetime] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        hosts: dict[str, dict[str, Any]] = {}
        for key, (day, n) in self.day_count.items():
            hosts.setdefault(key, {})["day"] = day.isoformat()
            hosts[key]["count"] = n
        for host, at in self.last_request.items():
            hosts.setdefault(host, {})["last_request"] = iso(at)
        for host, s in self.backoff_s.items():
            hosts.setdefault(host, {})["backoff_s"] = s
        for host, at in self.blocked_until.items():
            hosts.setdefault(host, {})["blocked_until"] = iso(at)
        sources = {name: iso(at) for name, at in self.source_last_request.items()}
        return {"format": FORMAT, "hosts": hosts, "sources": sources}

    @classmethod
    def from_json(cls, data: Any) -> HostLimits:
        out = cls()
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError("not a host-state file of a known format")
        hosts = data.get("hosts") or {}
        sources = data.get("sources") or {}
        if not isinstance(hosts, dict) or not isinstance(sources, dict):
            raise ValueError("hosts or sources is not a table")
        for host, row in hosts.items():
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("day"), str) and isinstance(row.get("count"), int):
                out.day_count[str(host)] = (date.fromisoformat(row["day"]), max(0, int(row["count"])))
            at = parse_iso(row.get("last_request"))
            if at is not None:
                out.last_request[str(host)] = at
            if isinstance(row.get("backoff_s"), (int, float)) and not isinstance(row.get("backoff_s"), bool):
                out.backoff_s[str(host)] = float(row["backoff_s"])
            until = parse_iso(row.get("blocked_until"))
            if until is not None:
                out.blocked_until[str(host)] = until
        for name, value in sources.items():
            at = parse_iso(value)
            if at is not None:
                out.source_last_request[str(name)] = at
        return out


class HostStateFile:
    """Loads and saves `HostLimits` at `path` (inside Scout's folder, via `paths`)."""

    def __init__(self, paths: Any, path: Path, log: logging.Logger | None = None) -> None:
        self.paths, self.path = paths, Path(path)
        self.log = log or logging.getLogger("scout.http")

    def load(self) -> HostLimits:
        from scout.store import read_json
        try:
            if not self.path.exists():
                return HostLimits()
            return HostLimits.from_json(read_json(self.path))
        except Exception as exc:  # noqa: BLE001 - a bad file never stops Scout
            self.log.warning("host-state file unreadable (%s); per-host limits start empty", type(exc).__name__)
            return HostLimits()

    def save(self, limits: HostLimits) -> None:
        from scout.store import atomic_write_json
        try:
            atomic_write_json(self.paths, self.path, limits.to_json())
        except Exception as exc:  # noqa: BLE001 - a failed save never stops a fetch
            self.log.warning("host-state file not saved (%s)", type(exc).__name__)
