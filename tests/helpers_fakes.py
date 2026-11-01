"""Fakes shared by every unit's tests (DESIGN 12). Nothing here touches the network.

- FakeResponse / FakeTransport: a transport for `scout.http.HttpFetcher` (url -> response); records
  every call (url and header names only: values may hold the contact email).
- FakeFetcher: implements interfaces.Fetcher from a url -> FetchResult (or bytes) map.
- FakeRawSink, FakeFactSink, FakeHealthSink, FakeBoard, FakeTickerMap, FakeSubmissions.
- write_records(scout_dir, kind, records): lays out state files for reader tests.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from scout_reader import records as R

from scout.clock import et_date, iso
from scout.models import Fact, FetchResult, HealthRecord, PeriodicInfo, SourceState

__all__ = [
    "FakeBoard", "FakeFactSink", "FakeFetcher", "FakeHealthSink", "FakeRawSink", "FakeResponse", "FakeSubmissions",
    "FakeTickerMap", "FakeTransport", "fetch_result", "write_records",
]


@dataclass
class FakeResponse:
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


class FakeTransport:
    """Callable(url, headers, timeout_s, max_bytes) -> FakeResponse. A url with no response, or an
    Exception given as the response, raises (OSError by default)."""

    def __init__(self, responses: Mapping[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.calls: list[tuple[str, tuple[str, ...], float, int]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout_s: float, max_bytes: int) -> FakeResponse:
        self.calls.append((url, tuple(sorted(headers)), timeout_s, max_bytes))
        response = self.responses.get(url)
        if response is None:
            raise OSError(f"no fake response for {url}")
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, list):  # a queue of responses
            item = response.pop(0) if len(response) > 1 else response[0]
            if isinstance(item, BaseException):
                raise item
            return item
        return response


def fetch_result(source: str, url: str, body: bytes | None = b"", *, result: str = "ok", status: int | None = 200,
                 at: datetime | None = None) -> FetchResult:
    stamp = iso(at) if at is not None else "2026-11-02T14:00:00Z"
    return FetchResult(source=source, url=url, result=result, status=status, body=body, headers={},
                       started_at=stamp, ended_at=stamp, bytes=len(body or b""), not_modified=result == "not_modified")


class FakeFetcher:
    """implements Fetcher: url -> FetchResult or bytes (a 200)."""

    def __init__(self, results: Mapping[str, FetchResult | bytes] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[tuple[str, str]] = []

    def fetch(self, source: str, url: str, *, conditional: bool = True, max_bytes: int | None = None) -> FetchResult:
        self.calls.append((source, url))
        got = self.results.get(url)
        if got is None:
            return fetch_result(source, url, None, result="network", status=None)
        if isinstance(got, bytes):
            return fetch_result(source, url, got)
        return got


class FakeRawSink:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, bytes, datetime]] = []

    def save(self, source: str, name: str, body: bytes, at: datetime) -> str:
        self.saved.append((source, name, body, at))
        return hashlib.sha256(body).hexdigest()


class FakeFactSink:
    def __init__(self) -> None:
        self.facts: list[Fact] = []

    def add(self, fact: Fact) -> bool:
        if self.seen(fact.fact_id):
            return False
        self.facts.append(fact)
        return True

    def seen(self, fact_id: str) -> bool:
        return any(f.fact_id == fact_id for f in self.facts)


class FakeHealthSink:
    def __init__(self) -> None:
        self.records: list[HealthRecord] = []

    def record(self, rec: HealthRecord) -> None:
        self.records.append(rec)


class FakeBoard:
    def __init__(self) -> None:
        self.sources: dict[str, SourceState] = {}
        self.llm: dict[str, Any] = {}
        self.levels: dict[str, Any] = {}

    def update(self, source: str, state: SourceState) -> None:
        self.sources[source] = state

    def set_llm(self, enabled: bool, spent_today_usd: float, cap_reached: bool) -> None:
        self.llm = {"enabled": enabled, "spent_today_usd": spent_today_usd, "cap_reached": cap_reached}

    def set_levels(self, current_files: list[str], expires: str | None) -> None:
        self.levels = {"current_files": list(current_files), "expires": expires}


class FakeTickerMap:
    def __init__(self, rows: Mapping[int, tuple[str, str | None]] | None = None) -> None:
        self.rows = dict(rows or {})

    def ticker_for_cik(self, cik: int) -> tuple[str, str | None] | None:
        return self.rows.get(cik)

    def cik_for_ticker(self, ticker: str) -> int | None:
        for cik, (t, _ex) in self.rows.items():
            if t.upper() == ticker.upper():
                return cik
        return None


class FakeSubmissions:
    def __init__(self, infos: Mapping[str, PeriodicInfo] | None = None) -> None:
        self.infos = dict(infos or {})
        self.calls: list[str] = []

    def periodic_info(self, ticker: str, now: datetime) -> PeriodicInfo:
        self.calls.append(ticker)
        return self.infos.get(ticker.upper(), PeriodicInfo(ticker=ticker.upper(), cik=None, status="no_cik"))


# --------------------------------------------------------------------------- state files

_FOLDERS = {"facts": R.FACTS, "health": R.HEALTH, "beats": R.BEATS, "answers": R.ANSWERS, "labels": R.LABELS,
            "llm": R.LLM, "due": R.EDGAR_DUE}
_TIME_KEYS = ("first_seen_at", "start", "at", "as_of", "computed_at", "labelled_at")
_SINGLE = {"params": R.PARAMS, "jason": R.JASON_SNAPSHOTS, "macro": R.MACRO_HISTORY}


def _main_time(rec: Mapping[str, Any]) -> datetime:
    for key in _TIME_KEYS:
        at = R.parse_time(rec.get(key))
        if at is not None:
            return at
    raise ValueError(f"record has no main time: {sorted(rec)}")


def write_records(scout_dir: Path | str, kind: str, records: Iterable[Mapping[str, Any]], *,
                  raw_lines: Iterable[str] = ()) -> list[Path]:
    """Append `records` (format 1 added when missing) to the files a writer would use: daily kinds
    (facts, health, beats, answers, labels, llm, due) by the ET date of the main time; single-file
    kinds (params, jason, macro) to their one file. `raw_lines` are appended verbatim afterwards
    (to test partial and corrupt lines)."""
    import json  # noqa: PLC0415

    root = Path(scout_dir)
    written: list[Path] = []
    pending: dict[Path, list[str]] = {}  # one open per file, lines kept in order
    for rec in records:
        body = {"format": 1, **dict(rec)}
        if kind in _SINGLE:
            path = root / _SINGLE[kind]
        elif kind in _FOLDERS:
            path = root / _FOLDERS[kind] / f"{et_date(_main_time(body)).isoformat()}.jsonl"
        else:
            raise ValueError(f"unknown kind {kind!r}")
        pending.setdefault(path, []).append(json.dumps(body, sort_keys=True) + "\n")
        if path not in written:
            written.append(path)
    for path, lines in pending.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines))
    for line in raw_lines:
        if not written:
            raise ValueError("raw_lines need at least one record to choose the file")
        with open(written[-1], "a", encoding="utf-8", newline="") as fh:
            fh.write(line)
    return written
