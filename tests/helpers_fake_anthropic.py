"""A fake Claude API for the LLM tests (SCOUT_SPEC 2.11, 14). Nothing here touches the network.

`FakeAnthropic` implements `interfaces.LlmTransport`: `create(**params)` records a deep copy of
the params and returns the next queued Message-like object (an Exception in the queue is raised).
Messages carry `content` blocks (attribute objects, as the SDK's), `stop_reason`, `usage` (with
`server_tool_use`), `model` and `_request_id`, including `web_search_tool_result` and
`web_fetch_tool_result` blocks shaped like the API's.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Sequence
from typing import Any

__all__ = [
    "Block", "FakeAnthropic", "FakeMessage", "fact", "fetch_result", "j1_reply", "label", "make_ctx", "message",
    "search_result", "server_tool_use", "text", "usage",
]


class Block:
    """An SDK-like attribute object (no model_dump, like a plain pydantic-free object)."""

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)

    def __repr__(self) -> str:
        return f"Block({vars(self)!r})"


class FakeMessage(Block):
    pass


def usage(input_tokens: int = 1000, output_tokens: int = 200, cache_creation: int = 0, cache_read: int = 0,
          searches: int = 0, fetches: int = 0) -> Block:
    return Block(input_tokens=input_tokens, output_tokens=output_tokens, cache_creation_input_tokens=cache_creation,
                 cache_read_input_tokens=cache_read,
                 server_tool_use=Block(web_search_requests=searches, web_fetch_requests=fetches))


def text(value: Any) -> Block:
    return Block(type="text", text=value if isinstance(value, str) else json.dumps(value))


def server_tool_use(name: str, tool_id: str, **input_: Any) -> Block:
    return Block(type="server_tool_use", id=tool_id, name=name, input=dict(input_))


def search_result(tool_id: str, urls: Iterable[str]) -> Block:
    return Block(type="web_search_tool_result", tool_use_id=tool_id,
                 content=[Block(type="web_search_result", url=u, title=f"title {u}", encrypted_content="xx",
                                page_age=None) for u in urls])


def fetch_result(tool_id: str, url: str, page_text: str) -> Block:
    doc = Block(type="document", title="page", citations=None,
                source=Block(type="text", media_type="text/plain", data=page_text))
    return Block(type="web_fetch_tool_result", tool_use_id=tool_id,
                 content=Block(type="web_fetch_result", url=url, content=doc, retrieved_at="2026-11-02T00:30:00Z"))


def message(content: Sequence[Any] | Any, *, stop_reason: str = "end_turn", model: str = "claude-haiku-5-5",
            request_id: str = "req_fake_1", use: Block | None = None) -> FakeMessage:
    """A Message. A dict/list `content` becomes one text block holding its JSON."""
    if not isinstance(content, (list, tuple)) or (content and not isinstance(content[0], Block)):
        blocks = [text(content)]
    else:
        blocks = list(content)
    msg = FakeMessage(id="msg_fake", type="message", role="assistant", content=blocks, stop_reason=stop_reason,
                      model=model, usage=use or usage())
    msg._request_id = request_id  # set after init: private on the SDK object too
    return msg


class FakeAnthropic:
    """Queue of replies (Message, Exception, or callable(params) -> Message)."""

    def __init__(self, *replies: Any) -> None:
        self.replies: list[Any] = list(replies)
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(copy.deepcopy(params))
        if not self.replies:
            raise AssertionError("FakeAnthropic: no reply queued")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(params)
        return reply


# --------------------------------------------------------------------------- Scout-side factories


def fact(ticker: str | None, title: str, first_seen_at: str, *, fid: str | None = None, kind: str = "wire",
         cls: str | None = "contract", source: str = "prnewswire", guid: str | None = None, url: str | None = None,
         published_at: str | None = None, form: str | None = None, items: Sequence[str] = (),
         halt_time: str | None = None, halt_code: str | None = None, resumed_at: str | None = None) -> dict[str, Any]:
    """A full Fact record (DESIGN 5.1)."""
    from scout.models import Fact  # noqa: PLC0415

    import hashlib  # noqa: PLC0415

    fid = fid or "fact-" + hashlib.sha256(f"{ticker}|{title}|{first_seen_at}".encode()).hexdigest()[:16]
    return Fact(fact_id=fid, kind=kind, ticker=ticker, cik=None, cls=cls, source=source, feed=source,
                guid=guid or fid, url=url, title=title, published_at=published_at or first_seen_at,
                first_seen_at=first_seen_at, raw_sha256=None, form=form, items=tuple(items), halt_time=halt_time,
                halt_code=halt_code, resumed_at=resumed_at, halt_market="NASDAQ" if kind == "halt" else None
                ).to_record()


def label(ticker: str, evidence: Sequence[str], **over: Any) -> dict[str, Any]:
    """A schema-valid label (SS 8.3)."""
    base = {"ticker": ticker, "catalyst_type": "contract_or_partnership", "direction": "positive",
            "dilution_risk": "none", "is_new_information": True, "scheduled_event": None, "confidence": "med",
            "evidence_ids": list(evidence), "summary": "A contract award."}
    base.update(over)
    return base


def j1_reply(*pairs: tuple[str, dict[str, Any]], **kw: Any) -> FakeMessage:
    return message({"labels": [{"item_id": i, "label": lab} for i, lab in pairs]}, **kw)


def make_ctx(cfg: Any, paths: Any, clock: Any, transport: Any = None) -> Any:
    import logging  # noqa: PLC0415

    from scout.app import AppContext  # noqa: PLC0415
    from scout.heartbeat import SourceStatusBoard  # noqa: PLC0415

    ctx = AppContext(cfg=cfg, paths=paths, clock=clock, board=SourceStatusBoard(cfg), log=logging.getLogger("test.llm"))
    ctx._services["llm_transport"] = transport
    return ctx
