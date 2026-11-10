"""The reads file and the checks on an answer (READS_TEST.md 5.3, 5.4).

`state/reads/<trading day>.jsonl`: one line per read (`first`, `reread`, `owner`) or per pack a
selected reader did not read (`skip`). Append-only; `first_seen_at` is Scout's clock at the moment
the line is written; every write goes through `ScoutPaths.writable` (`scout.store.append_jsonl`).
`read_id` is `<trading day>:<symbol>:<n>`, n counting every read of that symbol that day from 1.
The writer is shared by Scout's loop (the owner's notes) and the reads worker (the model's reads),
so one lock covers the numbering and the append.

`check_answer` applies 5.4 to the model's answer: it must match the schema; `horizon_min` must be 5
to 390 and there must be at least one reason (else the answer is invalid). Notes longer than 120
characters and `fade_if` longer than 160 are cut, and reasons past the sixth are dropped. For a long,
`invalidation` must be below the ask and `target` above it; for a short, `invalidation` above the
bid and `target` below it; a price on the wrong side (or not a positive number) is set to null.
When the side's quote is missing the price is kept as given. Prices are stored as strings.

Nothing that answers a question reads these files (owner, 2026-11-10; `tests/test_reads_isolation.py`).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scout.clock import Clock
from scout.llm_schemas import validate
from scout.paths import ScoutPaths
from scout.reads_prompt import FIRST_SCHEMA, PROMPT_VERSION, REREAD_SCHEMA, fmt_num
from scout.store import append_jsonl, read_jsonl

FORMAT = 1
ANSWER_FIELDS = ("direction", "strength", "horizon_min", "invalidation", "target", "reasons", "fade_if", "status")
SKIP_WHYS = ("cap", "stale", "error", "refusal", "invalid")
HORIZON_RANGE = (5, 390)
MAX_REASONS, NOTE_CHARS, FADE_CHARS = 6, 120, 160

__all__ = ["ANSWER_FIELDS", "SKIP_WHYS", "ReadsWriter", "check_answer", "dec", "iso_us", "money", "read_line",
           "reads_on", "skip_line"]


def iso_us(dt: datetime) -> str:
    """UTC ISO-8601 with microseconds and "Z" (READS_TEST.md 3)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def dec(value: Any) -> Decimal | None:
    """A positive finite price, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() and out > 0 else None


def money(value: Any) -> str:
    """USD as a decimal string, to 8 places at most ("0.000965", "0.0"): READS_TEST.md 3 keeps money
    in strings."""
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    text = f"{amount if math.isfinite(amount) else 0.0:.8f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _price_text(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        return None
    text = fmt_num(float(value))
    return text if text not in ("?", "-") else None


def check_answer(obj: Any, *, reread: bool, ref_bid: Any, ref_ask: Any) -> tuple[dict[str, Any] | None, str]:
    """(the answer as stored, "") or (None, the problem)."""
    problems = validate(obj, REREAD_SCHEMA if reread else FIRST_SCHEMA)
    if problems:
        return None, problems[0][:120]
    lo, hi = HORIZON_RANGE
    if not lo <= obj["horizon_min"] <= hi:
        return None, "horizon_min out of range"
    if not obj["reasons"]:
        return None, "no reasons"
    out = {k: obj[k] for k in ANSWER_FIELDS}
    out["reasons"] = [{"kind": r["kind"], "side": r["side"], "note": " ".join(r["note"].split())[:NOTE_CHARS]}
                      for r in obj["reasons"][:MAX_REASONS]]
    out["fade_if"] = " ".join(obj["fade_if"].split())[:FADE_CHARS]
    inv, tgt = _price_text(obj["invalidation"]), _price_text(obj["target"])
    bid, ask = dec(ref_bid), dec(ref_ask)
    if obj["direction"] == "long" and ask is not None:
        inv = inv if inv is not None and Decimal(inv) < ask else None
        tgt = tgt if tgt is not None and Decimal(tgt) > ask else None
    elif obj["direction"] == "short" and bid is not None:
        inv = inv if inv is not None and Decimal(inv) > bid else None
        tgt = tgt if tgt is not None and Decimal(tgt) < bid else None
    out["invalidation"], out["target"] = inv, tgt
    return out, ""


def _quote(pack: Mapping[str, Any]) -> Mapping[str, Any]:
    q = pack.get("quote")
    return q if isinstance(q, Mapping) else {}


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def read_line(kind: str, reader: Any, pack: Mapping[str, Any], answer: Mapping[str, Any], *, read_id: str,
              parent_read_id: str | None, facts_used: list[str], usage: Mapping[str, int] | None,
              cost_usd: float, market_shown: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A `first` or `reread` line (without `first_seen_at`, which the writer adds). `market_shown`
    (0.3.2) is the market block the reader was shown (`scout.reads_market`), or null."""
    q = _quote(pack)
    rec: dict[str, Any] = {
        "format": FORMAT, "kind": kind, "read_id": read_id, "reader": reader.reader_id, "model": reader.model,
        "effort": reader.effort, "prompt_version": PROMPT_VERSION, "symbol": pack.get("symbol"),
        "trading_day": pack.get("trading_day"), "session": pack.get("session"),
        "candidate_id": pack.get("candidate_id"), "pack_id": pack.get("pack_id"), "pack_made_at": pack.get("made_at"),
        "ref_bid": _str_or_none(q.get("bid")), "ref_ask": _str_or_none(q.get("ask")),
    }
    rec.update({k: answer.get(k) for k in ANSWER_FIELDS})
    rec.update({"parent_read_id": parent_read_id, "facts_used": list(facts_used), "usage": dict(usage or {}),
                "cost_usd": money(cost_usd), "market_shown": _shown(market_shown)})
    return rec


def _shown(block: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not block:
        return None
    symbols = block.get("symbols") if isinstance(block.get("symbols"), Mapping) else {}
    return {"source": block.get("source"), "feed": block.get("feed"), "at": block.get("at"),
            "symbols": {str(s): {"last": v.get("last"), "change_pct": v.get("change_pct")}
                        for s, v in symbols.items() if isinstance(v, Mapping)}}


def skip_line(reader: Any, pack: Mapping[str, Any], why: str, *, for_kind: str, detail: str = "",
              cost_usd: float = 0.0) -> dict[str, Any]:
    """A `skip` line: `kind`, `reader`, `pack_id`, `first_seen_at` (added by the writer) and `why`,
    plus keys for the audit (`for` first or reread, what the call cost, a short detail)."""
    if why not in SKIP_WHYS:
        raise ValueError(f"unknown skip reason {why!r}")
    return {"format": FORMAT, "kind": "skip", "reader": reader.reader_id, "pack_id": pack.get("pack_id"),
            "why": why, "for": for_kind, "symbol": pack.get("symbol"), "trading_day": pack.get("trading_day"),
            "candidate_id": pack.get("candidate_id"), "model": reader.model, "effort": reader.effort,
            "prompt_version": PROMPT_VERSION, "detail": detail[:120] or None, "cost_usd": money(cost_usd)}


def reads_on(paths: ScoutPaths, day: str | date, *, strict: bool = False) -> list[dict[str, Any]]:
    """The day's lines ([] when there is no file). With `strict`, a file that exists but cannot be
    opened raises OSError instead of reading as empty."""
    path = paths.reads / f"{day if isinstance(day, str) else day.isoformat()}.jsonl"
    if strict:
        try:
            with open(path, "rb"):
                pass
        except FileNotFoundError:
            return []
    return read_jsonl(path)


class ReadsWriter:
    def __init__(self, paths: ScoutPaths, clock: Clock) -> None:
        self.paths, self.clock = paths, clock
        self._lock = threading.Lock()
        self._counts: dict[str, dict[str, int]] = {}

    def file(self, day: str) -> Path:
        return self.paths.reads / f"{day}.jsonl"

    def _count(self, day: str, symbol: str) -> int:
        if day not in self._counts:
            counts: dict[str, int] = {}
            for rec in read_jsonl(self.file(day)):
                parts = str(rec.get("read_id") or "").split(":")
                if len(parts) == 3 and parts[2].isdigit():
                    counts[parts[1]] = max(counts.get(parts[1], 0), int(parts[2]))
            self._counts[day] = counts
            for old in sorted(self._counts)[:-3]:
                del self._counts[old]
        return self._counts[day].get(symbol, 0)

    def append(self, rec: dict[str, Any], *, number: bool) -> dict[str, Any]:
        """Append `rec` to its trading day's file, stamping `first_seen_at` (and, with `number`, the
        next `read_id`) under the lock. Returns the line as written."""
        day = str(rec.get("trading_day") or "")
        if len(day) != 10:
            raise ValueError("a read line needs its trading_day")
        with self._lock:
            out = dict(rec)
            if number:
                symbol = str(out.get("symbol") or "")
                n = self._count(day, symbol) + 1
                out["read_id"] = f"{day}:{symbol}:{n}"
            out["first_seen_at"] = iso_us(self.clock.now())
            append_jsonl(self.paths, self.file(day), out)
            if number:
                self._counts[day][symbol] = n
        return out
