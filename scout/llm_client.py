"""The Claude API transport and the shared call flow (SCOUT_SPEC 8.1, DESIGN 2.6).

- `build_transport(ctx)` returns None unless `llm_enabled` is true and `ANTHROPIC_API_KEY` is set.
  `anthropic` is imported here only, lazily, with `max_retries=0` (Scout retries at most once
  itself) and `llm_timeout_s`. The key is passed to the SDK and never logged, printed, stored on a
  Scout object or written to a file.
- `call(transport, request)` sends `request.params()` and normalises the SDK's Message into plain
  data (`LlmResponse`). An SDK exception becomes `LlmApiError` carrying the exception type only.
- `execute(...)` is the flow every job shares: ledger check, a `reserved` record at the estimate
  (refused, never sent, when it cannot be written), call, on a refusal, `max_tokens` or a schema
  failure at most one retry, a record per call in `state/llm/<date>.jsonl` that settles the
  reservation. A timeout or a connection error is recorded with `cost_usd` = the pre-call estimate
  and its searches (reason `charged_estimate:<type>`): the server may have finished and billed the
  call. When the final record cannot be written the reservation stands at the worst case.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from scout.clock import Clock, iso
from scout.llm_prompts import LlmRequest
from scout.llm_schemas import SCHEMA_VERSION, validate
from scout.models import LlmCall
from scout.paths import ScoutPaths

log = logging.getLogger("scout.llm")

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
              "web_search_requests", "web_fetch_requests")
RESULT_BLOCKS = ("web_search_tool_result", "web_fetch_tool_result")

__all__ = [
    "USAGE_KEYS", "AnthropicTransport", "LlmApiError", "LlmResponse", "Outcome", "build_transport", "call",
    "execute", "maybe_billed", "plain", "usage_dict",
]


class LlmApiError(RuntimeError):
    """A failed API call. The message is the exception type only (no request, header or key)."""


class AnthropicTransport:
    """Implements interfaces.LlmTransport over an `anthropic.Anthropic` client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def create(self, **params: Any) -> Any:
        return self._client.messages.create(**params)

    def __repr__(self) -> str:
        return "AnthropicTransport()"


def build_transport(ctx: Any) -> AnthropicTransport | None:
    cfg = ctx.cfg
    if not cfg.llm_enabled:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    os.environ.pop("ANTHROPIC_LOG", None)  # SDK debug logging could print request headers
    import anthropic  # noqa: PLC0415 - lazy: only when the LLM is on

    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=float(cfg.llm_timeout_s))
    return AnthropicTransport(client)


# --------------------------------------------------------------------------- normalising


def plain(obj: Any, depth: int = 0) -> Any:
    """SDK objects (pydantic models or simple attribute objects) as JSON-able data."""
    if depth > 30:
        return None
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Mapping):
        return {str(k): plain(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v, depth + 1) for v in obj]
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return plain(dump(), depth + 1)
        except Exception:  # noqa: BLE001 - fall through to attributes
            pass
    if hasattr(obj, "__dict__"):
        return {k: plain(v, depth + 1) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def usage_dict(usage: Any) -> dict[str, int]:
    u = plain(usage) or {}
    server = u.get("server_tool_use") or {}
    out: dict[str, int] = {}
    for key in USAGE_KEYS:
        value = server.get(key) if key.startswith("web_") else u.get(key)
        try:
            out[key] = max(0, int(value or 0))
        except (TypeError, ValueError):
            out[key] = 0
    return out


@dataclass(frozen=True)
class LlmResponse:
    blocks: tuple[dict[str, Any], ...]
    stop_reason: str | None
    usage: dict[str, int]
    request_id: str | None
    model: str | None

    @property
    def text(self) -> str:
        """The last text block (the structured output follows any tool use)."""
        texts = [b.get("text") for b in self.blocks if b.get("type") == "text" and isinstance(b.get("text"), str)]
        return texts[-1] if texts else ""

    def result_blocks(self) -> list[dict[str, Any]]:
        return [b for b in self.blocks if b.get("type") in RESULT_BLOCKS]


def call(transport: Any, request: LlmRequest) -> LlmResponse:
    try:
        msg = transport.create(**request.params())
    except Exception as exc:  # noqa: BLE001 - never pass SDK text (could echo headers) upwards
        raise LlmApiError(type(exc).__name__) from None
    blocks = tuple(b for b in (plain(x) for x in (getattr(msg, "content", None) or [])) if isinstance(b, dict))
    rid = getattr(msg, "_request_id", None) or getattr(msg, "id", None)
    return LlmResponse(blocks=blocks, stop_reason=getattr(msg, "stop_reason", None),
                       usage=usage_dict(getattr(msg, "usage", None)), request_id=str(rid) if rid else None,
                       model=getattr(msg, "model", None))


# --------------------------------------------------------------------------- the shared flow


class LedgerLike(Protocol):
    def allow(self, job: str, request: LlmRequest) -> tuple[bool, str]: ...

    def estimate(self, request: LlmRequest) -> float: ...

    def price(self, usage: Mapping[str, Any], model: str) -> float: ...


@dataclass
class Outcome:
    status: str  # "ok" | "unlabelled" | "refused"
    obj: Any = None
    response: LlmResponse | None = None
    validation: str = ""
    reason: str = ""
    cost_usd: float = 0.0
    usage: dict[str, int] = field(default_factory=lambda: {k: 0 for k in USAGE_KEYS})
    request_id: str | None = None
    calls: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def maybe_billed(error_type: str) -> bool:
    """True when a failed call may still have run (and been billed) on the server: a timeout or a
    connection that broke after the request went out. Such a call is charged its pre-call
    worst-case estimate in the ledger, so the daily and monthly caps still see the spend (a J3
    research loop can outlast `llm_timeout_s` and finish on the server anyway)."""
    name = error_type.lower()
    return "timeout" in name or "connection" in name


def _parse(response: LlmResponse, schema: Mapping[str, Any]) -> tuple[str, Any]:
    if response.stop_reason == "refusal":
        return "refusal", None
    if response.stop_reason == "max_tokens":
        return "max_tokens", None
    try:
        obj = json.loads(response.text)
    except ValueError:
        return "schema_fail", None
    if validate(obj, schema):
        return "schema_fail", None
    return "ok", obj


def _record(paths: ScoutPaths, request: LlmRequest, at: datetime, *, validation: str, retry: bool,
            estimate: float, response: LlmResponse | None = None, cost: float = 0.0, reason: str = "",
            usage: Mapping[str, int] | None = None, extra: dict[str, Any] | None = None) -> None:
    from scout.llm_records import write_call  # noqa: PLC0415 - avoid an import cycle

    if response is not None:
        used = dict(response.usage)
    else:
        used = {k: 0 for k in USAGE_KEYS}
        used.update(usage or {})
    rec = LlmCall(job=request.job, at=iso(at), model_id=request.model, effort=request.effort,
                  prompt_sha256=request.prompt_sha256, schema_version=SCHEMA_VERSION,
                  request_id=response.request_id if response else None, usage=used,
                  cost_usd=round(cost, 8), estimate_usd=round(estimate, 8),
                  stop_reason=response.stop_reason if response else None, validation=validation, retry=retry,
                  fact_ids=tuple(request.fact_ids), symbols=tuple(request.symbols))
    write_call(paths, rec, reason=reason or None, extra=extra)


def _worst_usage(request: LlmRequest) -> dict[str, int]:
    """The usage a sent call may have run up on the server before Scout heard back."""
    return {"web_search_requests": request.web_search_uses(), "web_fetch_requests": request.web_fetch_uses()}


def _note(paths: ScoutPaths, request: LlmRequest, clock: Clock, **kw: Any) -> None:
    """A record of a call that was not sent (refused): best effort, nothing was billed."""
    try:
        _record(paths, request, clock.now(), **kw)
    except Exception as exc:  # noqa: BLE001 - the refusal stands whether or not it is written
        log.warning("LLM %s: refusal record not written (%s)", request.job, type(exc).__name__)


def _settle(paths: ScoutPaths, request: LlmRequest, clock: Clock, rid: str, **kw: Any) -> None:
    """The final record of a reserved call. A failed write leaves the reservation (the worst case)
    in the ledger and is not raised: the call has been made, and raising would only send it again."""
    try:
        _record(paths, request, clock.now(), extra={"settles": rid}, **kw)
    except Exception as exc:  # noqa: BLE001 - the reservation already counts the call at its worst case
        log.warning("LLM %s: call record not written (%s); its reservation stands", request.job,
                    type(exc).__name__)


def execute(transport: Any, request: LlmRequest, ledger: LedgerLike, paths: ScoutPaths, clock: Clock) -> Outcome:
    """Check the ledger, call, retry once on refusal / max_tokens / schema failure, record each call.
    Never raises for an API failure (that is recorded as `api_error`)."""
    out = Outcome(status="unlabelled")
    for attempt in range(2):
        allowed, reason = ledger.allow(request.job, request)
        estimate = ledger.estimate(request)
        if not allowed:
            if reason not in ("llm_disabled", "job_disabled"):
                _note(paths, request, clock, validation="refused_by_cap", retry=attempt > 0, estimate=estimate,
                      reason=reason)
            out.reason = reason
            if attempt == 0:
                out.status = "refused"
            return out
        if transport is None:
            _note(paths, request, clock, validation="no_key", retry=attempt > 0, estimate=estimate)
            out.status, out.reason = "refused", "no_key"
            return out
        rid = uuid.uuid4().hex
        try:
            _record(paths, request, clock.now(), validation="reserved", retry=attempt > 0, estimate=estimate,
                    cost=estimate, usage=_worst_usage(request), extra={"reservation": rid})
        except Exception as exc:  # noqa: BLE001 - no reservation, no call: the caps could not see it
            log.warning("LLM %s: ledger not writable (%s); call not sent", request.job, type(exc).__name__)
            out.reason = "ledger_unwritable"
            if attempt == 0:
                out.status = "refused"
            return out
        try:
            response = call(transport, request)
        except LlmApiError as exc:
            log.warning("LLM %s call failed: %s", request.job, exc)
            billed = maybe_billed(str(exc))
            charged = estimate if billed else 0.0
            _settle(paths, request, clock, rid, validation="api_error", retry=attempt > 0, estimate=estimate,
                    cost=charged, reason=f"charged_estimate:{exc}" if charged else "",
                    usage=_worst_usage(request) if billed else None)
            out.validation, out.reason, out.calls = "api_error", str(exc), out.calls + 1
            out.cost_usd += charged
            return out
        cost = ledger.price(response.usage, request.model)
        validation, obj = _parse(response, request.schema)
        _settle(paths, request, clock, rid, validation=validation, retry=attempt > 0, estimate=estimate,
                response=response, cost=cost)
        out.calls += 1
        out.cost_usd += cost
        out.usage = {k: out.usage.get(k, 0) + response.usage.get(k, 0) for k in USAGE_KEYS}
        out.response, out.validation, out.request_id = response, validation, response.request_id
        if validation == "ok":
            out.status, out.obj = "ok", obj
            return out
    return out
