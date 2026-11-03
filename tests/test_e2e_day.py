"""End to end: one simulated trading day through `python -m scout run`'s loop (DESIGN 8, 12).

Monday 2026-11-02 from 03:55 to 20:40 ET on a ManualClock (the quiet core session from 09:30 to
15:30, where only polling happens, is skipped to keep the test fast; gate 6's window starts at the
16:00 close, so the evening answers still need clean coverage). Scout is built from a config.toml on
disk (`build_context`), every job module is loaded strictly, and the only fakes are the HTTP
transport (every source answered from the offline fixtures) and the Claude transport (a scripted
fake; no key, no network). The config keeps the defaults: gate 6 runs on EDGAR and PR Newswire, as on
the owner's PC (GlobeNewswire was removed on 2026-11-03). Radar and Pilot are stand-in folders. The day must produce facts, health,
the heartbeat, macro files, the Jason snapshot, answers, the brief (with J4) and the verify report,
and the CLI must then answer from those files.
"""

from __future__ import annotations

import json
import logging
import re
import socket
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_EMAIL, et_at
from helpers_fake_anthropic import message
from helpers_fakes import FakeResponse, FakeTransport
from helpers_macro import BLS_URL, HALTS_URL, VIX_URL, bls_data_results, halts_body, macro_body, nowflation_results
from helpers_wire import fixture, full_transport

from scout import cli
from scout.app import AppContext, build_context, load_jobs, run
from scout.clock import ManualClock, parse_iso
from scout.http import HttpFetcher
from scout.store import read_jsonl

DAY = "2026-11-02"
START_ET, END_ET = "03:55", "20:40"
STEP_S = 60.0  # one loop tick per simulated minute keeps the day under ~30 s on Windows
DATED_HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts&haltdate=11022026"


LOOP_S = 10.0  # loop_interval_s in the day's config.toml (its maximum)


QUIET = ("09:30", "15:30")  # the core session, where nothing but polling happens, is skipped


class StepClock(ManualClock):
    """A ManualClock whose main-loop sleep (`LOOP_S`) advances `STEP_S` instead, so the day runs in
    fewer ticks (the 30 s heartbeat is checked tick by tick in the failing-source run below), and
    which jumps over the `QUIET` core session (every append costs ~4 ms on Windows). Every other
    sleep (per-host spacing) is exact."""

    def sleep(self, seconds: float) -> None:
        self.advance(STEP_S if float(seconds) == LOOP_S else float(seconds))
        if et_at(DAY, QUIET[0]) <= self.now() < et_at(DAY, QUIET[1]):
            self.set(et_at(DAY, QUIET[1]))


class ScriptedClaude:
    """A Claude transport that answers from the request: J1 labels every item with the ticker in
    its text; J4 writes a fixed paragraph; J3 returns an uncited label (never verified)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _user(params: Mapping[str, Any]) -> str:
        msgs = params.get("messages") or []
        content = msgs[0].get("content") if msgs else ""
        if isinstance(content, list):
            return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, Mapping))
        return str(content)

    @staticmethod
    def _label(ticker: str, evidence: list[str]) -> dict[str, Any]:
        return {"ticker": ticker, "catalyst_type": "none", "direction": "neutral", "dilution_risk": "none",
                "is_new_information": True, "scheduled_event": None, "confidence": "low",
                "evidence_ids": evidence, "summary": "A scripted test label."}

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        system = json.dumps(params.get("system"))
        user = self._user(params)
        if "Scout J1" in system:
            labels = []
            for item_id, body in re.findall(r'<item id="([^"]+)" nonce="[^"]+">\n(.*?)\n</item', user, re.S):
                ticker = re.search(r"ticker: (\S+)", body)
                if ticker:
                    labels.append({"item_id": item_id, "label": self._label(ticker.group(1), [item_id])})
            return message({"labels": labels})
        if "Scout J4" in system:
            return message({"paragraph": "A quiet scripted morning.", "watch_list": []})
        ticker = re.search(r"Input ticker set: ([A-Z.\-]+)", user)
        return message({"label": self._label(ticker.group(1) if ticker else "NONE", ["F1"]), "citations": []})


@dataclass
class Day:
    scout_dir: Path
    config: Path
    ctx: AppContext
    clock: StepClock
    http: FakeTransport
    claude: ScriptedClaude
    rc: int
    others_before: dict[str, bytes]

    def jsonl(self, *parts: str) -> list[dict[str, Any]]:
        return read_jsonl(self.scout_dir.joinpath("state", *parts))


EVENING_ET = "17:00"  # from here PR Newswire's feed carries one new release (after the close)
EVENING_ITEM = (
    "<item><title>Mu Devices Announces FDA Approval of Its Second Device</title>"
    "<link>https://www.prnewswire.com/news-releases/mu-devices-second-approval-302900099.html</link>"
    "<guid>https://www.prnewswire.com/news-releases/mu-devices-second-approval-302900099.html</guid>"
    "<pubDate>Mon, 02 Nov 2026 22:00:00 +0000</pubDate>"
    "<description><![CDATA[<p>MINNEAPOLIS, Nov. 2, 2026 /PRNewswire/ -- Mu (NASDAQ: MUDV) today announced news. "
    "Synthetic text for tests.</p>]]></description></item>")


def _evening_feed() -> bytes:
    """The PR Newswire fixture with the evening release on top and its oldest item dropped (the
    feed keeps 20 items, so the overlap check still sees the earlier ones)."""
    text = fixture("prnewswire.rss").decode("utf-8")
    first = text.index("<item>")
    last = text.rindex("<item>")
    text = text[:last] + text[text.index("</item>", last) + len("</item>"):]
    return (text[:first] + EVENING_ITEM + text[first:]).encode("utf-8")


class DayTransport(FakeTransport):
    """The fixtures, with PR Newswire's evening feed served from `EVENING_ET` on."""

    def __init__(self, base: FakeTransport, clock: ManualClock, prn_url: str) -> None:
        super().__init__(base.responses)
        self.clock, self.prn_url = clock, prn_url

    def __call__(self, url: str, headers: Mapping[str, str], timeout_s: float, max_bytes: int) -> FakeResponse:
        if url == self.prn_url and self.clock.now() >= et_at(DAY, EVENING_ET):
            self.calls.append((url, tuple(sorted(headers)), timeout_s, max_bytes))
            return FakeResponse(200, {}, _evening_feed())
        return super().__call__(url, headers, timeout_s, max_bytes)


def _http(cfg: Any) -> FakeTransport:
    tr = full_transport(cfg)
    ok = lambda body: FakeResponse(200, {}, body)  # noqa: E731
    for url, body in nowflation_results().items():
        tr.responses[url] = ok(body)
    tr.responses[BLS_URL] = ok(macro_body("bls_SYNTHETIC.ics"))
    tr.responses[VIX_URL] = ok(macro_body("VIX_History_head_SYNTHETIC.csv"))
    for url, body in bls_data_results().items():
        tr.responses[url] = ok(body)
    tr.responses[HALTS_URL] = ok(halts_body())
    tr.responses[DATED_HALTS_URL] = ok(halts_body("tradehalts_empty_SYNTHETIC.xml"))
    return tr


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def _write_setup(root: Path) -> tuple[Path, Path]:
    scout_dir, radar, pilot = root / "Scout", root / "Radar", root / "Pilot"
    (radar / "state" / "handoff").mkdir(parents=True)
    (radar / "state" / "handoff" / f"{DAY}.jsonl").write_text(
        json.dumps({"format": 1, "candidate_id": "c1", "symbol": "MUDV", "found_at": "2026-11-02T15:00:00Z", "price": 4.2}) + "\n",
        encoding="utf-8")
    (pilot / "state" / "plans").mkdir(parents=True)
    (pilot / "state" / "plans" / "p1.json").write_text(json.dumps({"symbol": "ACME", "status": "open"}),
                                                       encoding="utf-8")
    scout_dir.mkdir()
    config = scout_dir / "config.toml"
    config.write_text("\n".join([
        f'contact_email = "{TEST_EMAIL}"',
        f"radar_dir = '{radar}'",
        f"pilot_dir = '{pilot}'",
        'watch_symbols = ["BETA", "WXYZ"]',
        'edgar_atom_forms = ["8-K", "424B", "NT 10-K"]',
        "llm_enabled = true",
        "loop_interval_s = 10.0",
        "",
    ]), encoding="utf-8")
    return scout_dir, config


@pytest.fixture(scope="module")
def day(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Day]:
    root = tmp_path_factory.mktemp("e2e")
    scout_dir, config = _write_setup(root)
    others = _snapshot(root / "Radar") | {f"pilot/{k}": v for k, v in _snapshot(root / "Pilot").items()}
    with pytest.MonkeyPatch.context() as mp:
        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"network access in the e2e day: {args!r}")
        mp.setattr(socket.socket, "connect", refuse)
        mp.setattr(socket, "getaddrinfo", refuse)
        for key in [k for k in __import__("os").environ if k.upper().startswith("ANTHROPIC_")]:
            mp.delenv(key)
        clock = StepClock(et_at(DAY, START_ET))
        ctx = build_context(config, clock=clock, log=logging.getLogger("scout.e2e"))
        http = DayTransport(_http(ctx.cfg), clock, ctx.cfg.source("prnewswire").url)
        claude = ScriptedClaude()
        ctx._services["fetcher"] = HttpFetcher(ctx.cfg, clock, transport=http, log=ctx.log.getChild("http"))
        ctx._services["llm_transport"] = claude
        jobs = load_jobs(ctx, strict=True)
        ticks = int((et_at(DAY, END_ET) - et_at(DAY, START_ET)).total_seconds() // STEP_S)
        rc = run(ctx, max_ticks=ticks, jobs=jobs, until=et_at(DAY, END_ET))
    yield Day(scout_dir, config, ctx, clock, http, claude, rc, others)


# --------------------------------------------------------------------------- the loop


def test_the_day_runs_and_stops_cleanly(day: Day) -> None:
    assert day.rc == 0
    assert day.clock.now() >= et_at(DAY, END_ET) - timedelta(seconds=STEP_S)
    hb = json.loads((day.scout_dir / "state" / "scout.heartbeat").read_text(encoding="utf-8"))
    assert hb["stopped"] is True


def test_heartbeat_all_day_with_every_source(day: Day) -> None:
    beats = [b for p in sorted((day.scout_dir / "state" / "beats").glob("*.jsonl")) for b in read_jsonl(p)
             if not b.get("stopped")]
    times = [parse_iso(b["at"]) for b in beats]
    assert len(beats) > 600  # 03:55-09:30 and 15:30-20:40 ET, one beat per tick
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)
            if not (et_at(DAY, QUIET[0]) <= b <= et_at(DAY, QUIET[1]) + timedelta(minutes=2))]
    assert max(gaps) <= 2 * STEP_S
    noon = next(b for b in beats if parse_iso(b["at"]) >= et_at(DAY, "09:00"))
    for source in ("edgar_atom", "prnewswire", "nasdaq_halts", "edgar_tickers", "bls", "cboe_vix", "nowflation"):
        assert noon["sources"][source] == "ok", source
    assert not any(noon["gap_open"].values())
    # GlobeNewswire was removed on 2026-11-03 (it refuses automated readers): never reported or fetched
    assert "globenewswire" not in noon["sources"]
    assert not any("globenewswire" in url for url, *_ in day.http.calls)
    params = day.jsonl("params.jsonl")
    assert params[0]["gate6_sources"] == ["edgar_atom", "prnewswire", "stocktitan"]


def test_only_known_urls_and_no_failed_source(day: Day) -> None:
    unknown = sorted({url for url, *_ in day.http.calls if url not in day.http.responses})
    assert unknown == []
    health = day.jsonl("health", f"{DAY}.jsonl")
    assert health
    results = Counter(h["result"] for h in health)
    assert set(results) <= {"ok", "not_modified"}, results


def test_one_failing_source_never_stops_the_others(day: Day, tmp_path: Path) -> None:
    """A second short run, at the real 10 s loop, where one feed raises: the others still poll,
    and the loop keeps beating every 30 s."""
    clock = ManualClock(et_at(DAY, "10:00"))
    folder = tmp_path / "Scout"
    folder.mkdir()
    cfg_text = day.config.read_text(encoding="utf-8").replace("llm_enabled = true", "llm_enabled = false")
    (folder / "config.toml").write_text(cfg_text, encoding="utf-8")
    ctx = build_context(folder / "config.toml", clock=clock, log=logging.getLogger("scout.e2e.fail"))
    http = _http(ctx.cfg)
    http.responses[ctx.cfg.source("prnewswire").url] = RuntimeError("feed exploded")
    ctx._services["fetcher"] = HttpFetcher(ctx.cfg, clock, transport=http, log=ctx.log.getChild("http"))
    assert run(ctx, until=et_at(DAY, "10:10"), jobs=load_jobs(ctx, strict=True)) == 0
    health = read_jsonl(folder / "state" / "health" / f"{DAY}.jsonl")
    by_source = Counter((h["source"], h["result"]) for h in health)
    assert by_source[("edgar_atom", "ok")] >= 8  # the other gate-6 source keeps polling
    assert by_source[("nasdaq_halts", "ok")] >= 8
    assert not any(src == "prnewswire" and res == "ok" for src, res in by_source)
    beats = read_jsonl(folder / "state" / "beats" / f"{DAY}.jsonl")
    live = [parse_iso(b["at"]) for b in beats if not b.get("stopped")]
    assert len(live) >= 18
    assert all(25 <= (b - a).total_seconds() <= 45 for a, b in zip(live, live[1:], strict=False))
    assert any(b.get("sources", {}).get("prnewswire") not in (None, "ok") for b in beats if not b.get("stopped"))


# --------------------------------------------------------------------------- what the day wrote


def test_facts_of_every_kind(day: Day) -> None:
    facts = day.jsonl("facts", f"{DAY}.jsonl")
    kinds = Counter(f["kind"] for f in facts)
    for kind in ("wire", "filing", "halt", "macro_event"):
        assert kinds[kind] > 0, kinds
    assert len({f["fact_id"] for f in facts}) == len(facts)  # never stored twice across 2,000 polls
    assert any(f["kind"] == "wire" and f["ticker"] == "MUDV" and f["class"] == "fda" for f in facts)


def test_macro_files(day: Day) -> None:
    daily = json.loads((day.scout_dir / "state" / "macro" / f"{DAY}.json").read_text(encoding="utf-8"))
    assert daily["inflation_regime"] == "steady"
    assert daily["vix"] == {"close": 17.12, "date": "2026-10-30"}
    assert {"nowflation", "bls", "fomc", "cboe_vix"} <= set(daily["sources"])
    assert day.jsonl("macro", "history.jsonl")


def test_jason_snapshot_and_params(day: Day) -> None:
    assert day.jsonl("jason", "snapshots.jsonl")
    params = day.jsonl("params.jsonl")
    assert len(params) == 1 and params[0]["scout_stale_s"] == 120


def test_answers_for_pilot_watch_and_radar(day: Day) -> None:
    answers = day.jsonl("answers", f"{DAY}.jsonl")
    by = {(a["origin"], a["symbol"], a["as_of"][11:15]) for a in answers}  # "HH:M" in UTC
    assert ("pilot", "ACME", "00:3") in by and ("pilot", "ACME", "00:4") in by  # the 19:30 and 19:45 ET runs
    assert all(et_at(DAY, "19:30") <= parse_iso(a["as_of"]) <= et_at(DAY, "20:40") for a in answers)
    assert any(o == "watch" and s == "BETA" for o, s, _ in by)
    assert any(o == "radar" and s == "MUDV" for o, s, _ in by)
    for a in answers:
        assert a["stale"] is False
        for gate in ("fresh_catalyst", "event_before_next_session", "halt_open", "news_headline"):
            assert a[gate] in ("true", "false", "unknown")
    mudv = next(a for a in answers if a["symbol"] == "MUDV")
    assert mudv["fresh_catalyst"] == "true"  # the 17:00 ET fda release, inside gate 6's window
    acme = next(a for a in answers if a["symbol"] == "ACME")
    # the 08:30 ET results release and 08:31 ET 8-K 2.02 of the same day: gate 6's window starts at
    # the prior trading day's (Friday's) close, not at today's 16:00 (SS 7.1)
    assert acme["fresh_catalyst"] == "true"
    assert acme["reasons"]["fresh_catalyst"] == ["fact_in_window"]
    beta = next(a for a in answers if a["symbol"] == "BETA")
    assert beta["event_before_next_session"] == "unknown"  # a recent NT filing


def test_brief_with_every_section_and_j4(day: Day) -> None:
    text = (day.scout_dir / "reports" / f"scout-{DAY}.md").read_text(encoding="utf-8")
    for heading in ("## Macro events", "## Inflation regime and VIX", "## Overnight catalysts", "## Open halts",
                    "## Jason's levels", "## Source health"):
        assert heading in text, heading
    assert "Inflation regime: steady" in text  # the 08:20 nowflation read is in the 08:20 brief
    assert "VIX close: 17.12" in text
    assert "nfp" in text and "sources agree" in text
    assert "**MUDV** [fda]" in text
    assert "A quiet scripted morning." in text
    assert "https://nowflation.com" in text


def test_claude_calls_recorded_and_labels_kept_apart(day: Day) -> None:
    jobs = Counter("J1" if "Scout J1" in json.dumps(c.get("system")) else
                   "J4" if "Scout J4" in json.dumps(c.get("system")) else "J3" for c in day.claude.calls)
    assert jobs["J1"] >= 1 and jobs["J4"] == 1 and jobs["J3"] >= 1
    calls = [r for p in sorted((day.scout_dir / "state" / "llm").glob("**/*.jsonl")) for r in read_jsonl(p)]
    assert len(calls) >= len(day.claude.calls)
    assert not any("api_key" in json.dumps(c).lower() for c in day.claude.calls)


def test_verify_report_and_sample(day: Day) -> None:
    assert (day.scout_dir / "reports" / f"scout-verify-{DAY}.md").exists()
    assert day.jsonl("verify", "samples.jsonl")


def test_nothing_written_outside_scouts_folder(day: Day) -> None:
    root = day.scout_dir.parent
    after = _snapshot(root / "Radar") | {f"pilot/{k}": v for k, v in _snapshot(root / "Pilot").items()}
    assert after == day.others_before
    assert sorted(p.name for p in root.iterdir()) == ["Pilot", "Radar", "Scout"]


# --------------------------------------------------------------------------- the CLI on the day's files


@pytest.fixture
def cli_clock(day: Day, monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    clock = ManualClock(et_at(DAY, "21:00"))
    monkeypatch.setattr("scout.app.SystemClock", lambda: clock)
    return clock


def test_cli_answers_reads_the_days_files(day: Day, cli_clock: ManualClock,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    before = _snapshot(day.scout_dir / "state" / "answers")
    rc = cli.main(["--config", str(day.config), "answers", "MUDV", "BETA", "--as-of", "2026-11-03T00:45:00Z"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MUDV" in out and "BETA" in out
    assert _snapshot(day.scout_dir / "state" / "answers") == before  # the answers command writes nothing


def test_cli_brief_rewrites_the_days_brief(day: Day, cli_clock: ManualClock) -> None:
    assert cli.main(["--config", str(day.config), "brief", "--date", DAY]) == 0
    text = (day.scout_dir / "reports" / f"scout-{DAY}.md").read_text(encoding="utf-8")
    assert "Inflation regime: steady" in text  # rebuilt as of 08:20 from the macro history
