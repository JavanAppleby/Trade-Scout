"""Record shapes (SCOUT_SPEC 10, DESIGN 5). Frozen dataclasses with `to_record()` (adds `format`,
writes `null` for an absent value, never omits a key) and `from_record(d)` (raises ValueError on a
missing key or an unknown format; extra keys are ignored, since keys are only ever added).

Python names that clash with keywords are mapped: `cls` <-> "class".
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Literal, Self

from scout import FORMAT

Tri = Literal["true", "false", "unknown"]

#: "discord" (one Paper Gains Discord post per ticker) and "earnings_schedule" (one listed earnings date per
#: ticker) come from the inbox capture files (DESIGN 2.7); neither is a gate-6 kind. "scanner" (one Paper Gains
#: scanner card per ticker) too, and no gate reads it at all.
FACT_KINDS = ("wire", "filing", "halt", "scheduled_event", "macro_event", "discord", "earnings_schedule", "scanner")
WIRE_CLASSES = ("offering", "reverse_split", "listing_notice", "bankruptcy", "fda", "trial", "contract",
                "earnings_result", "earnings_date", "other")  # SS 6.4 order, `other` last; bankruptcy: owner, 2026-11-04
FILING_CLASSES = ("contract_or_other", "earnings_result", "listing_notice", "offering", "reverse_split",
                  "late_filing", "bankruptcy", "other")
SOURCE_STATUSES = ("ok", "stale", "unavailable", "disabled", "refused", "starting")
HEALTH_RESULTS = ("ok", "not_modified", "http_error", "timeout", "network", "schema", "too_large", "refused",
                  "backoff", "no_http", "host_cap")
KNOWN_FORMATS = frozenset({FORMAT})

__all__ = [
    "FACT_KINDS", "FILING_CLASSES", "HEALTH_RESULTS", "SOURCE_STATUSES", "WIRE_CLASSES", "AnswerRecord", "Beat",
    "DueWindowRecord", "Fact", "FetchResult", "Filing", "HealthRecord", "HeartbeatBody", "JasonSnapshot", "Label",
    "LevelMeta", "LlmCall", "MacroRecord", "PeriodicInfo", "Record", "SourceState", "Tri",
]


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Record):
        return value.to_record()
    return value


class Record:
    """Mixin for the record dataclasses."""

    RENAME: ClassVar[dict[str, str]] = {}  # python field -> JSON key
    TUPLES: ClassVar[frozenset[str]] = frozenset()  # fields stored as lists, held as tuples
    HAS_FORMAT: ClassVar[bool] = True
    ADDED: ClassVar[frozenset[str]] = frozenset()  # keys added later: absent in older records, read as the default

    def to_record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"format": FORMAT} if self.HAS_FORMAT else {}
        for f in fields(self):  # type: ignore[arg-type]
            out[self.RENAME.get(f.name, f.name)] = _plain(getattr(self, f.name))
        return out

    @classmethod
    def from_record(cls, d: Any) -> Self:
        if not isinstance(d, dict):
            raise ValueError(f"{cls.__name__}: a record must be a JSON object")
        if cls.HAS_FORMAT:
            fmt = d.get("format")
            if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt not in KNOWN_FORMATS:
                raise ValueError(f"{cls.__name__}: unknown format {fmt!r}")
        kwargs: dict[str, Any] = {}
        for f in fields(cls):  # type: ignore[arg-type]
            key = cls.RENAME.get(f.name, f.name)
            if key not in d and f.name in cls.ADDED:
                continue
            if key not in d:
                raise ValueError(f"{cls.__name__}: missing key {key!r}")
            value = d[key]
            if f.name in cls.TUPLES:
                if not isinstance(value, list):
                    raise ValueError(f"{cls.__name__}: {key!r} must be a list")
                value = tuple(value)
            kwargs[f.name] = value
        return cls(**kwargs)


# --------------------------------------------------------------------------- facts and health


@dataclass(frozen=True)
class Fact(Record):
    """`state/facts/<date>.jsonl` (DESIGN 5.1)."""

    RENAME: ClassVar[dict[str, str]] = {"cls": "class"}
    TUPLES: ClassVar[frozenset[str]] = frozenset({"items"})

    fact_id: str
    kind: str
    ticker: str | None
    cik: int | None
    cls: str | None
    source: str
    feed: str
    guid: str
    url: str | None
    title: str | None
    published_at: str | None
    first_seen_at: str
    raw_sha256: str | None
    exchange: str | None = None
    form: str | None = None
    items: tuple[str, ...] = ()
    accession: str | None = None
    halt_code: str | None = None
    halt_time: str | None = None
    resumed_at: str | None = None
    halt_market: str | None = None
    event_at: str | None = None
    event_kind: str | None = None
    event_source: str | None = None
    sources_agree: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in FACT_KINDS:
            raise ValueError(f"Fact: unknown kind {self.kind!r}")


@dataclass(frozen=True)
class HealthRecord(Record):
    """`state/health/<date>.jsonl` (DESIGN 5.2): one per poll."""

    ADDED: ClassVar[frozenset[str]] = frozenset({"seen_items"})

    source: str
    feed: str
    kind: str
    enabled: bool
    gate6: bool
    start: str
    end: str
    result: str
    http_status: int | None
    items: int
    new_items: int
    overlap: bool | None
    spacing_s: float
    gap_before: bool
    error: str | None = None
    # 0.2.2 (owner, 2026-11-04): distinct ids on a good page already seen in this feed (a 304: 0, its
    # overlap is by result); null for a failed poll and in records written before 0.2.2
    seen_items: int | None = None

    def __post_init__(self) -> None:
        if self.result not in HEALTH_RESULTS:
            raise ValueError(f"HealthRecord: unknown result {self.result!r}")


# --------------------------------------------------------------------------- heartbeat


@dataclass(frozen=True)
class SourceState(Record):
    """One source in the heartbeat (SS 10.1)."""

    HAS_FORMAT: ClassVar[bool] = False

    enabled: bool
    status: str = "starting"
    last_ok: str | None = None
    last_error: str | None = None
    gap_open: bool = False

    def __post_init__(self) -> None:
        if self.status not in SOURCE_STATUSES:
            raise ValueError(f"SourceState: unknown status {self.status!r}")


@dataclass(frozen=True)
class Beat(Record):
    """`state/beats/<date>.jsonl` (DESIGN D4, 5.3): one line per heartbeat."""

    at: str
    stopped: bool
    sources: dict[str, str]
    gap_open: dict[str, bool]


@dataclass(frozen=True)
class HeartbeatBody(Record):
    """`state/scout.heartbeat` (SS 10.1, DESIGN 5.3)."""

    at: str
    version: str
    stopped: bool
    sources: dict[str, dict[str, Any]]
    llm: dict[str, Any]
    levels: dict[str, Any]
    calendar_covers_next_45d: bool


# --------------------------------------------------------------------------- answers and EDGAR


@dataclass(frozen=True)
class AnswerRecord(Record):
    """`state/answers/<date>.jsonl` (DESIGN 5.5)."""

    symbol: str
    as_of: str
    written_at: str
    origin: str
    fresh_catalyst: str
    event_before_next_session: str
    macro_event_next_session: str
    news_headline: str
    halt_open: str
    reasons: dict[str, list[str]]
    evidence: dict[str, list[str]]
    stale: bool


@dataclass(frozen=True)
class Filing(Record):
    """One EDGAR filing (Radar's Filing plus the report date, DESIGN 2.2). Not a file record."""

    HAS_FORMAT: ClassVar[bool] = False
    TUPLES: ClassVar[frozenset[str]] = frozenset({"items"})

    form: str
    accepted: str
    filing_date: str
    accession: str
    items: tuple[str, ...] = ()
    report_date: str | None = None


@dataclass(frozen=True)
class PeriodicInfo(Record):
    """What `SubmissionsSource.periodic_info` returns (DESIGN 2.1 interfaces). Never raised."""

    HAS_FORMAT: ClassVar[bool] = False
    TUPLES: ClassVar[frozenset[str]] = frozenset({"filings"})

    ticker: str
    cik: int | None
    status: str  # ok | no_cik | ambiguous_ticker | fetch_failed | schema | no_http
    filings: tuple[Filing, ...] = ()
    fiscal_year_end: str | None = None
    fetched_at: str | None = None


@dataclass(frozen=True)
class DueWindowRecord(Record):
    """`state/edgar/due/<date>.jsonl` (DESIGN 5.6)."""

    ticker: str
    cik: int | None
    computed_at: str
    status: str
    last_periodic_form: str | None
    last_periodic_accession: str | None
    report_date: str | None
    fiscal_year_end: str | None
    next_period_end: str | None
    next_is_fiscal_year_end: bool | None
    due_start: str | None
    due_end: str | None
    overdue: bool | None
    nt_recent: bool | None
    nt_accession: str | None
    submissions_fetched_at: str | None


# --------------------------------------------------------------------------- LLM


@dataclass(frozen=True)
class Label(Record):
    """`state/labels/<date>.jsonl` (SS 8.3, DESIGN 5.7)."""

    TUPLES: ClassVar[frozenset[str]] = frozenset({"evidence_ids", "check_failures", "urls", "quotes"})

    job: str
    fact_id: str | None
    status: str  # labelled | unlabelled
    ticker: str | None
    catalyst_type: str | None
    direction: str | None
    dilution_risk: str | None
    is_new_information: bool | None
    scheduled_event: Any
    confidence: str | None
    evidence_ids: tuple[str, ...]
    summary: str | None
    verified: bool
    check_failures: tuple[str, ...]
    model_id: str | None
    prompt_sha256: str | None
    schema_version: int
    request_id: str | None
    usage: dict[str, Any]
    cost_usd: float
    labelled_at: str
    urls: tuple[str, ...] = ()
    quotes: tuple[str, ...] = ()
    published_at: str | None = None
    trust: str = "layer2"


@dataclass(frozen=True)
class LlmCall(Record):
    """`state/llm/<date>.jsonl` (DESIGN 5.7): one per Claude API call (or refusal before one)."""

    TUPLES: ClassVar[frozenset[str]] = frozenset({"fact_ids", "symbols"})

    job: str
    at: str
    model_id: str
    effort: str | None
    prompt_sha256: str | None
    schema_version: int
    request_id: str | None
    usage: dict[str, Any]
    cost_usd: float
    estimate_usd: float
    stop_reason: str | None
    validation: str
    retry: bool
    fact_ids: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()


# --------------------------------------------------------------------------- macro and levels


@dataclass(frozen=True)
class MacroRecord(Record):
    """`state/macro/history.jsonl` (DESIGN 5.8): one per source read."""

    TUPLES: ClassVar[frozenset[str]] = frozenset({"reasons", "events", "forecast_lean"})

    source: str
    first_seen_at: str
    fetched_at: str
    status: str
    reasons: tuple[str, ...]
    source_generated_at: str | None
    sha256: str | None
    events: tuple[dict[str, Any], ...]
    realtime: dict[str, Any] | None
    inflation_regime: str | None
    forecast_lean: tuple[dict[str, Any], ...]
    vix: dict[str, Any] | None
    attribution: str | None
    results: dict[str, Any] | None = None  # bls_data only (scout.bls_data); additive key


@dataclass(frozen=True)
class LevelMeta(Record):
    """`state/levels/<session>.<kind>.meta.json` (DESIGN 5.9)."""

    TUPLES: ClassVar[frozenset[str]] = frozenset({"warnings"})

    added_at: str
    sha256: str
    original_filename: str
    session_date: str
    source_kind: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class JasonSnapshot(Record):
    """`state/jason/snapshots.jsonl` (DESIGN 5.9)."""

    TUPLES: ClassVar[frozenset[str]] = frozenset({"files", "setups", "warnings"})

    first_seen_at: str
    today_et: str
    files: tuple[str, ...]
    setups: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- HTTP


@dataclass(frozen=True)
class FetchResult(Record):
    """What `Fetcher.fetch` returns (DESIGN 10). Never raised; not a file record."""

    HAS_FORMAT: ClassVar[bool] = False

    source: str
    url: str
    result: str
    status: int | None
    body: bytes | None
    headers: dict[str, str]
    started_at: str
    ended_at: str
    bytes: int
    not_modified: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.result == "ok"
