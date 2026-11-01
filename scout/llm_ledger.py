"""The spend ledger and caps (SCOUT_SPEC 8.6, DESIGN 2.6).

Prices come from `[llm.prices]` (USD per million tokens, plus `web_search_usd_per_1000`); a model
with no price row is refused. Before every call the worst case is estimated and a call that would
cross `llm_daily_cost_cap_usd` or `llm_monthly_cost_cap_usd` is refused, as is one past
`j1_max_calls_per_day`, `j3_max_calls_per_day` or `web_search_max_per_day`.

The worst case of a call with server tools is a loop: each tool use is one more sampling
iteration, and each iteration reads the prompt again plus every result so far (search results and
fetched pages are billed as input every time). With n = searches + fetches, base = input
characters / 3.5 and r = the larger of a fetch's `max_content_tokens` and `SEARCH_RESULT_TOKENS`,
input = (n + 1) x base + r x n(n + 1)/2 tokens, priced at the dearer of input and cache-write;
plus `max_tokens` output and the searches' fee. A call with no tools is base + `max_tokens`.

Each call is reserved before it is sent (`execute` writes a `reserved` record at the estimate, with
its searches): a call whose final record cannot be written still counts at its worst case, and a
call whose reservation cannot be written is not sent. The final record `settles` its reservation.
A day file that exists but cannot be read refuses every call (`ledger_unreadable`): it never reads
as nothing spent.

At a cost cap every LLM job stops for the ET day; at a job cap that job stops. Layer 1, the answers
and the deterministic brief never consult the ledger, so they carry on.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from scout.clock import Clock, et_date
from scout.config import ScoutConfig
from scout.llm_prompts import LlmRequest
from scout.paths import ScoutPaths
from scout.llm_records import RESERVED, settled
from scout.store import read_jsonl

CHARS_PER_TOKEN = 3.5
SEARCH_RESULT_TOKENS = 10_000  # worst-case input tokens of one web_search result (billed as input)
SENT = ("ok", "schema_fail", "refusal", "max_tokens", "api_error", RESERVED)  # calls sent (or about to be)
COST_CAPS = ("daily_cap", "monthly_cap")
JOB_CAPS: dict[str, tuple[str, ...]] = {"J1": ("j1_cap",), "J3": ("j3_cap", "web_search_cap"), "J4": ()}

__all__ = ["CHARS_PER_TOKEN", "COST_CAPS", "RESERVED", "SEARCH_RESULT_TOKENS", "Ledger", "LedgerUnreadable",
           "settled"]


class LedgerUnreadable(OSError):
    """A ledger day file exists but cannot be read: the spend is not known."""


def _read(path: Any) -> list[dict[str, Any]]:
    """The records of one ledger file, [] when it does not exist; raises LedgerUnreadable when it
    exists but cannot be opened (a sharing violation, a permissions error)."""
    try:
        with open(path, "rb"):
            pass
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LedgerUnreadable(type(exc).__name__) from None
    return read_jsonl(path)



def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class Ledger:
    def __init__(self, paths: ScoutPaths, cfg: ScoutConfig, clock: Clock) -> None:
        self.paths, self.cfg, self.clock = paths, cfg, clock

    # ------------------------------------------------------------------ reading

    def _today(self) -> date:
        return et_date(self.clock.now())

    def _records(self, day: date | None = None) -> list[dict[str, Any]]:
        return settled(_read(self.paths.day_file(self.paths.llm, day or self._today())))

    def spent_today(self) -> float:
        return sum(_num(r.get("cost_usd")) for r in self._records())

    def spent_month(self) -> float:
        today = self._today()
        total = 0.0
        try:
            files = sorted(self.paths.llm.glob(f"{today:%Y-%m}-*.jsonl"))
        except OSError as exc:
            raise LedgerUnreadable(type(exc).__name__) from None
        for path in files:
            total += sum(_num(r.get("cost_usd")) for r in settled(_read(path)))
        return total

    def calls_today(self, job: str) -> int:
        return sum(1 for r in self._records() if r.get("job") == job and r.get("validation") in SENT)

    def searches_today(self) -> int:
        return int(sum(_num((r.get("usage") or {}).get("web_search_requests")) for r in self._records()))

    # ------------------------------------------------------------------ pricing

    def price(self, usage: Mapping[str, Any], model: str) -> float:
        """USD for one response's usage. A model with no price row costs as the dearest priced
        model (it is refused before any call, so this only guards odd replies)."""
        p = self.cfg.price(model)
        if p is None:
            prices = list(self.cfg.llm_prices.values())
            if not prices:
                return 0.0
            p = max(prices, key=lambda x: x.output_per_mtok)
        tokens = (_num(usage.get("input_tokens")) * p.input_per_mtok
                  + _num(usage.get("output_tokens")) * p.output_per_mtok
                  + _num(usage.get("cache_creation_input_tokens")) * p.cache_write_per_mtok
                  + _num(usage.get("cache_read_input_tokens")) * p.cache_read_per_mtok) / 1_000_000
        return tokens + _num(usage.get("web_search_requests")) * self.cfg.web_search_usd_per_1000 / 1000

    def estimate(self, request: LlmRequest) -> float:
        """Worst case for one call (the tool loop in the module doc); 0.0 for a model with no price
        (that call is refused anyway)."""
        p = self.cfg.price(request.model)
        if p is None:
            return 0.0
        base = request.input_chars() / CHARS_PER_TOKEN
        fetches, searches = request.web_fetch_uses(), request.web_search_uses()
        n = fetches + searches
        per_fetch = request.web_fetch_content_tokens() / fetches if fetches else 0.0
        result = max(per_fetch, float(SEARCH_RESULT_TOKENS) if searches else 0.0)
        input_tokens = (n + 1) * base + result * n * (n + 1) / 2
        in_price = max(p.input_per_mtok, p.cache_write_per_mtok)
        return (input_tokens * in_price + request.max_tokens * p.output_per_mtok) / 1_000_000 \
            + request.web_search_uses() * self.cfg.web_search_usd_per_1000 / 1000

    # ------------------------------------------------------------------ decisions

    def allow(self, job: str, request: LlmRequest) -> tuple[bool, str]:
        try:
            return self._allow(job, request)
        except LedgerUnreadable:
            return False, "ledger_unreadable"

    def _allow(self, job: str, request: LlmRequest) -> tuple[bool, str]:
        cfg = self.cfg
        if not cfg.llm_enabled:
            return False, "llm_disabled"
        if job not in cfg.llm_jobs or not cfg.llm_job(job).enabled:
            return False, "job_disabled"
        if cfg.price(request.model) is None:
            return False, "no_price"
        if job == "J1" and self.calls_today("J1") >= cfg.j1_max_calls_per_day:
            return False, "j1_cap"
        if job == "J3" and self.calls_today("J3") >= cfg.j3_max_calls_per_day:
            return False, "j3_cap"
        searches = request.web_search_uses()
        if searches and self.searches_today() + searches > cfg.web_search_max_per_day:
            return False, "web_search_cap"
        cost = self.estimate(request)
        if self.spent_today() + cost > cfg.llm_daily_cost_cap_usd:
            return False, "daily_cap"
        if self.spent_month() + cost > cfg.llm_monthly_cost_cap_usd:
            return False, "monthly_cap"
        return True, "ok"

    def cap_reached(self, job: str | None = None) -> bool:
        """True once a cost cap was met or refused a call today (every job stops for the day); with
        `job`, also once that job's own cap refused a call today. An unreadable ledger counts as reached."""
        try:
            return self._cap_reached(job)
        except LedgerUnreadable:
            return True

    def _cap_reached(self, job: str | None) -> bool:
        cfg = self.cfg
        if self.spent_today() >= cfg.llm_daily_cost_cap_usd or self.spent_month() >= cfg.llm_monthly_cost_cap_usd:
            return True
        reasons = {r.get("reason") for r in self._records() if r.get("validation") == "refused_by_cap"}
        if reasons & set(COST_CAPS):
            return True
        if job is not None:
            if reasons & set(JOB_CAPS.get(job, ())):
                return True
            if job == "J1" and self.calls_today("J1") >= cfg.j1_max_calls_per_day:
                return True
            if job == "J3" and self.calls_today("J3") >= cfg.j3_max_calls_per_day:
                return True
        return False

    def status(self) -> dict[str, Any]:
        """For the brief and the heartbeat."""
        try:
            return self._status()
        except LedgerUnreadable:
            return {"enabled": self.cfg.llm_enabled, "spent_today_usd": None, "spent_month_usd": None,
                    "cap_reached": True, "calls_today": {}, "searches_today": None, "unreadable": True}

    def _status(self) -> dict[str, Any]:
        return {"enabled": self.cfg.llm_enabled, "spent_today_usd": round(self.spent_today(), 6),
                "spent_month_usd": round(self.spent_month(), 6), "cap_reached": self.cap_reached(),
                "calls_today": {j: self.calls_today(j) for j in ("J1", "J3", "J4")},
                "searches_today": self.searches_today()}
