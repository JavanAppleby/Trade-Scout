"""scout_reader entry_blocks (owner, 2026-11-04; SCOUT_SPEC 7.8, 10.3; DESIGN 3.9): known bad news
blocks Radar's buy signal, even when Scout is stale; staleness only turns `false` into `unknown`."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from scout_reader import entry_blocks as top_level_entry_blocks
from scout_reader.api import Reader, entry_blocks
from scout_reader.blocks import BLOCK_KINDS, lookback_start

from scout.clock import iso
from scout.config import ScoutConfig

from conftest import et_at
from helpers_answers import fact, write_beats, write_health, write_params
from helpers_fakes import write_records

AS_OF = et_at("2026-11-04", "10:00")  # Wednesday: trading days Mon 2, Tue 3 and Wed 4 Nov
START = et_at("2026-10-30", "20:00")  # Monday's trading day begins at Friday 20:00 ET
S = "ACME"


def _world(scout_dir: Path, cfg: ScoutConfig, as_of: datetime = AS_OF, *, health_since: datetime | None = None,
           macro: bool = True) -> None:
    write_params(scout_dir, cfg, as_of - timedelta(days=60), gate6_feeds={})
    write_beats(scout_dir, as_of - timedelta(seconds=90), as_of)
    write_health(scout_dir, health_since or START - timedelta(minutes=10), as_of, every=150.0)
    if macro:
        _nowflation(scout_dir, as_of - timedelta(hours=2), [])


def _nowflation(scout_dir: Path, seen: datetime, events: list[tuple[str, datetime]], *, status: str = "ok") -> None:
    write_records(scout_dir, "macro", [{
        "source": "nowflation", "first_seen_at": iso(seen), "fetched_at": iso(seen), "status": status,
        "stale_after": iso(seen + timedelta(hours=30)), "reasons": [],
        "events": [{"kind": k, "event_at": iso(at)} for k, at in events]}])


def _facts(scout_dir: Path, *facts: dict) -> None:
    write_records(scout_dir, "facts", facts)


def _halt(ticker: str, halted: datetime, seen: datetime, *, resumed: datetime | None = None, code: str = "T1",
          guid: str | None = None) -> dict:
    key = f"{ticker}|{halted.date()}|{iso(halted)}"
    return fact(ticker, published=halted, seen=seen, cls=None, kind="halt", source="nasdaq_halts", title="Acme Inc",
                guid=guid or key, halt_code=code, halt_time=iso(halted), resumed_at=iso(resumed) if resumed else None,
                halt_market="NASDAQ", extra={"halt_key": key, "row": {}})


def _ask(scout_dir: Path, symbol: str = S, as_of: datetime = AS_OF, **kw) -> dict:
    return Reader(scout_dir).entry_blocks(symbol, as_of, **kw)


# --------------------------------------------------------------------------- false and the window


def test_nothing_on_file_with_full_coverage_is_false(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    res = _ask(scout_dir)
    assert res["value"] == "false" and res["stale"] is False and res["reasons"] == ["no_block_full_coverage"]
    assert res["blocks"] == [] and res["evidence"] == []
    assert res["window"] == {"facts_from": iso(START), "first_trading_day": "2026-11-02",
                             "macro": [iso(AS_OF - timedelta(minutes=15)), iso(AS_OF + timedelta(minutes=5))]}


def test_lookback_counts_trading_days_back_from_as_ofs_trading_day() -> None:
    assert lookback_start(AS_OF, 3) == (datetime(2026, 11, 2).date(), START)
    assert lookback_start(AS_OF, 1) == (datetime(2026, 11, 4).date(), et_at("2026-11-03", "20:00"))
    # after 20:00 ET the trading day is the next one; a weekend is skipped
    assert lookback_start(et_at("2026-11-06", "20:30"), 1)[0] == datetime(2026, 11, 9).date()


@pytest.mark.parametrize("cls", ["offering", "reverse_split", "listing_notice", "bankruptcy"])
def test_each_blocking_class_on_file_is_true(scout_dir: Path, cfg: ScoutConfig, cls: str) -> None:
    _world(scout_dir, cfg)
    f = fact(S, published=AS_OF - timedelta(days=1), cls=cls, kind="filing", title=f"Acme {cls}")
    _facts(scout_dir, f)
    res = _ask(scout_dir)
    assert res["value"] == "true" and res["reasons"] == ["block_on_file"] and res["evidence"] == [f["fact_id"]]
    assert res["blocks"] == [{"kind": cls, "class": cls, "fact_id": f["fact_id"], "first_seen_at": f["first_seen_at"],
                              "title": f"Acme {cls}"}]
    assert _ask(scout_dir, S.lower())["value"] == "true"  # symbols are upper-cased
    assert _ask(scout_dir, "OTHR")["value"] == "false"


@pytest.mark.parametrize("cls", ["fda", "trial", "contract", "contract_or_other", "earnings_result", "earnings_date",
                                 "other", "late_filing", None])
def test_other_classes_never_block(scout_dir: Path, cfg: ScoutConfig, cls: str | None) -> None:
    _world(scout_dir, cfg)
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(hours=1), cls=cls))
    assert _ask(scout_dir)["value"] == "false"


def test_the_window_edge_is_fridays_20_et(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    _facts(scout_dir, fact(S, published=START - timedelta(seconds=1), cls="offering", title="too old"))
    assert _ask(scout_dir)["value"] == "false"
    _facts(scout_dir, fact(S, published=START - timedelta(hours=1), seen=START, cls="offering", title="Fri 20:00"))
    res = _ask(scout_dir)
    assert res["value"] == "true" and [b["title"] for b in res["blocks"]] == ["Fri 20:00"]
    # one trading day back: Monday's filing no longer counts
    assert _ask(scout_dir, lookback_days=1)["value"] == "false"


def test_no_look_ahead(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(minutes=5), seen=AS_OF + timedelta(seconds=1),
                           cls="offering"),
           fact(S, published=AS_OF + timedelta(minutes=1), seen=AS_OF - timedelta(minutes=1), cls="bankruptcy"))
    assert _ask(scout_dir)["value"] == "false"


def test_discord_scanner_and_labels_never_block(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(hours=1), cls="offering", kind="discord", source="discord"),
           fact(S, published=AS_OF - timedelta(hours=1), cls="offering", kind="scanner", source="discord"))
    write_records(scout_dir, "labels", [{"labelled_at": iso(AS_OF - timedelta(hours=1)), "ticker": S,
                                         "event_type": "offering_or_dilution", "verified": True}])
    assert _ask(scout_dir)["value"] == "false"


def test_a_424b2_stored_as_an_offering_never_blocks(scout_dir: Path, cfg: ScoutConfig) -> None:
    """Facts written before the 2026-11-04 class fix carry class offering for banks' 424B2 notes."""
    _world(scout_dir, cfg)
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(hours=1), cls="offering", kind="filing", form="424B2"),
           fact(S, published=AS_OF - timedelta(hours=1), cls="offering", kind="filing", form="424B8", title="b8"))
    assert _ask(scout_dir)["value"] == "false"
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(hours=1), cls="offering", kind="filing", form="424B5",
                           title="b5"))
    assert [b["title"] for b in _ask(scout_dir)["blocks"]] == ["b5"]


# --------------------------------------------------------------------------- stale or sources not ok


def test_bad_news_on_file_blocks_even_when_scout_is_stale(scout_dir: Path, cfg: ScoutConfig) -> None:
    write_params(scout_dir, cfg, AS_OF - timedelta(days=60), gate6_feeds={})
    write_beats(scout_dir, AS_OF - timedelta(minutes=30), AS_OF - timedelta(minutes=10))  # stale at AS_OF
    res = _ask(scout_dir)
    assert res["value"] == "unknown" and res["stale"] is True and "scout_stale" in res["reasons"]
    f = fact(S, published=AS_OF - timedelta(days=1), cls="offering", kind="filing")
    _facts(scout_dir, f)
    res = _ask(scout_dir)
    assert res["value"] == "true" and res["stale"] is True and res["evidence"] == [f["fact_id"]]
    assert res["reasons"] == ["block_on_file", "scout_stale"]


def test_no_params_and_no_scout_at_all_still_count_facts_on_file(tmp_path: Path) -> None:
    empty = tmp_path / "Scout"
    assert _ask(empty)["value"] == "unknown"
    write_records(empty, "facts", [fact(S, published=AS_OF - timedelta(hours=2), cls="reverse_split")])
    assert _ask(empty)["value"] == "true"


@pytest.mark.parametrize("source", ["prnewswire", "edgar_atom", "nasdaq_halts"])
def test_a_source_not_ok_makes_false_unknown_but_never_hides_a_block(scout_dir: Path, cfg: ScoutConfig,
                                                                     source: str) -> None:
    _world(scout_dir, cfg)
    statuses = {s: "ok" for s in ("edgar_atom", "prnewswire", "stocktitan", "nasdaq_halts", "nowflation", "bls")}
    statuses[source] = "unavailable"
    write_beats(scout_dir, AS_OF, AS_OF, sources=statuses)  # the latest beat at AS_OF
    res = _ask(scout_dir)
    assert res["value"] == "unknown" and f"source_not_ok:{source}" in res["reasons"]
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(hours=3), cls="listing_notice"))
    assert _ask(scout_dir)["value"] == "true"


def test_a_coverage_gap_in_the_lookback_window_is_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg, health_since=et_at("2026-11-02", "12:00"))  # nothing polled before Monday noon
    res = _ask(scout_dir)
    assert res["value"] == "unknown" and "gap:prnewswire:no_poll_at_start" in res["reasons"]
    assert _ask(scout_dir, lookback_days=1)["value"] == "false"  # Wednesday's window is covered


def test_a_gap_open_in_the_beat_is_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    write_beats(scout_dir, AS_OF, AS_OF, gap_open={"nasdaq_halts": True})
    assert _ask(scout_dir)["reasons"] == ["gap_open:nasdaq_halts"]


def test_a_damaged_file_turns_false_into_unknown_never_true(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    write_records(scout_dir, "facts", [fact("ZZZZ", published=AS_OF - timedelta(hours=1))], raw_lines=["{broken\n"])
    res = _ask(scout_dir)
    assert res["value"] == "unknown" and "unreadable_line:facts/2026-11-04.jsonl" in res["reasons"]
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(hours=1), cls="bankruptcy"))
    assert _ask(scout_dir)["value"] == "true"


# --------------------------------------------------------------------------- halts and macro


def test_an_open_halt_blocks_and_a_resumed_one_does_not(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    halted = AS_OF - timedelta(minutes=20)
    h = _halt(S, halted, halted + timedelta(seconds=40), code="LUDP")
    _facts(scout_dir, h)
    res = _ask(scout_dir)
    assert res["value"] == "true" and res["blocks"] == [{
        "kind": "halt_open", "class": "LUDP", "fact_id": h["fact_id"], "first_seen_at": h["first_seen_at"],
        "title": f"halted {iso(halted)} (LUDP)"}]
    resumed = AS_OF - timedelta(minutes=10)
    _facts(scout_dir, _halt(S, halted, resumed + timedelta(seconds=30), resumed=resumed,
                            guid=f"{h['guid']}|resumed|{iso(resumed)}"))
    assert _ask(scout_dir)["value"] == "false"
    assert _ask(scout_dir, as_of=resumed - timedelta(seconds=1))["value"] == "true"  # as of then, still halted


def test_an_older_unresolved_halt_is_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    halted = et_at("2026-11-03", "15:00")
    _facts(scout_dir, _halt(S, halted, halted + timedelta(seconds=30)))
    res = _ask(scout_dir)
    assert res["value"] == "unknown" and res["reasons"] == ["halt_unresolved_since:2026-11-03"]


@pytest.mark.parametrize("offset_min, blocked", [(5, True), (6, False), (-15, True), (-16, False), (0, True)])
def test_the_macro_window_is_5_minutes_before_to_15_after(scout_dir: Path, cfg: ScoutConfig, offset_min: int,
                                                          blocked: bool) -> None:
    _world(scout_dir, cfg, macro=False)
    at = AS_OF + timedelta(minutes=offset_min)
    _nowflation(scout_dir, AS_OF - timedelta(hours=2), [("cpi", at), ("pce", AS_OF)])
    res = _ask(scout_dir)
    assert res["value"] == ("true" if blocked else "false")
    if blocked:
        assert res["blocks"] == [{"kind": "macro_window", "class": "cpi", "fact_id": f"cpi@{iso(at)}(nowflation)",
                                  "first_seen_at": iso(AS_OF - timedelta(hours=2)),
                                  "title": f"cpi at {iso(at)} (nowflation)"}]


def test_the_fomc_decision_and_the_jobs_report_block(scout_dir: Path, cfg: ScoutConfig) -> None:
    fomc = et_at("2026-12-09", "13:57")  # the decision is at 14:00 ET on a table date
    _world(scout_dir, cfg, fomc, health_since=fomc - timedelta(days=6))
    res = _ask(scout_dir, as_of=fomc)
    assert res["value"] == "true" and [(b["kind"], b["class"]) for b in res["blocks"]] == [("macro_window", "fomc")]
    jobs = et_at("2026-11-06", "08:40")
    _nowflation(scout_dir, jobs - timedelta(hours=1), [("nfp", et_at("2026-11-06", "08:30"))])
    write_beats(scout_dir, jobs - timedelta(seconds=60), jobs)
    assert [b["class"] for b in _ask(scout_dir, as_of=jobs)["blocks"]] == ["nfp"]
    assert _ask(scout_dir, as_of=jobs, macro_after_min=5)["blocks"] == []


def test_a_stale_macro_calendar_makes_false_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg, macro=False)
    res = _ask(scout_dir)
    assert res["value"] == "unknown" and "no_record:nowflation" in res["reasons"]
    _nowflation(scout_dir, AS_OF - timedelta(hours=40), [])  # older than stale_after
    assert "stale:nowflation" in _ask(scout_dir)["reasons"]


# --------------------------------------------------------------------------- shape, safety and the cache


def test_never_raises_and_refuses_bad_input(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    naive = AS_OF.replace(tzinfo=None)
    for res in (_ask(scout_dir, as_of=naive), _ask(scout_dir, symbol=""), _ask(scout_dir, symbol=None),  # type: ignore[arg-type]
                entry_blocks(scout_dir, S, "not a time"), _ask(scout_dir, lookback_days="x")):  # type: ignore[arg-type]
        assert res["value"] in ("unknown", "false") and res["blocks"] == []
    assert _ask(scout_dir, as_of=naive)["reasons"] == ["naive_as_of"]
    future = Reader(scout_dir, now=lambda: AS_OF - timedelta(minutes=1)).entry_blocks(S, AS_OF)
    assert future["value"] == "unknown" and future["reasons"] == ["future_as_of"]


def test_the_module_function_takes_a_folder_or_a_reader(scout_dir: Path, cfg: ScoutConfig) -> None:
    _world(scout_dir, cfg)
    _facts(scout_dir, fact(S, published=AS_OF - timedelta(hours=1), cls="offering"))
    reader = Reader(scout_dir)
    assert entry_blocks(scout_dir, S, AS_OF) == entry_blocks(reader, S, AS_OF) == top_level_entry_blocks(reader, S, AS_OF)
    assert set(BLOCK_KINDS) == {"offering", "reverse_split", "listing_notice", "bankruptcy", "halt_open",
                                "macro_window"}


def _append_line(path: Path, rec: dict) -> None:
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps({"format": 1, **rec}) + "\n")


def test_answers_are_identical_with_and_without_the_cache(scout_dir: Path, cfg: ScoutConfig) -> None:
    """Owner, 2026-11-04 (appendix): the cache keys each day file on (path, size, mtime); as of any t the
    answers with and without it are the same, while the files grow, and after a file is rewritten."""
    _world(scout_dir, cfg, AS_OF + timedelta(hours=2))
    cached, fresh = Reader(scout_dir), Reader(scout_dir, cache=False)
    symbols = (S, "BETA", "GAMA")
    times = [AS_OF + timedelta(minutes=m) for m in (0, 10, 20, 40, 60, 90)]

    def same() -> None:
        for at in times:
            for sym in symbols:
                assert cached.entry_blocks(sym, at) == fresh.entry_blocks(sym, at), (sym, at)

    same()
    for i, at in enumerate(times):
        _facts(scout_dir, fact(symbols[i % 3], published=at - timedelta(minutes=1), seen=at,
                               cls=("offering", "fda", "bankruptcy")[i % 3], title=f"n{i}"),
               _halt("BETA", at, at + timedelta(seconds=5)) if i == 2 else fact("ZZ", published=at))
        same()
    facts_file = scout_dir / "state" / "facts" / "2026-11-04.jsonl"
    with open(facts_file, "ab") as fh:
        fh.write(b'{"format": 1, "kind": "wire", "ticker": "GAMA"')  # a writer mid-append
    same()
    with open(facts_file, "ab") as fh:
        fh.write(b', "class": "offering", "fact_id": "late", "first_seen_at": "' + iso(AS_OF).encode()
                 + b'", "published_at": "' + iso(AS_OF).encode() + b'", "title": "joined"}\n')
    same()
    assert "late" in cached.entry_blocks("GAMA", times[-1])["evidence"]
    assert cached.cache is not None and cached.cache.tail_reads > 0
    full = cached.cache.full_reads
    lines = facts_file.read_bytes().split(b"\n")
    facts_file.write_bytes(b"\n".join(line.replace(b'"offering"', b'"other"') for line in lines))  # rewritten
    same()
    assert cached.cache.full_reads > full
    assert all(b["kind"] != "offering" for b in cached.entry_blocks("GAMA", times[-1])["blocks"])


def _big_facts_day(scout_dir: Path, day: str, n: int) -> int:
    """About n facts in one ET day file, like 3 Nov 2026's (12,000 facts, 10.8 MB, mostly 424B2s)."""
    path = scout_dir / "state" / "facts" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    base = et_at(day, "06:00")
    lines = []
    for i in range(n):
        at = iso(base + timedelta(seconds=i))
        lines.append(json.dumps({
            "format": 1, "fact_id": f"{i:064d}", "kind": "filing", "ticker": f"T{i % 4000}", "cik": i, "class": "offering",
            "form": "424B2", "source": "edgar_atom", "feed": "424B", "guid": f"g{i}", "url": "https://www.sec.gov/x" * 4,
            "title": "424B2 - A Bank Holding Corp (0000000001) (Filer)", "published_at": at, "first_seen_at": at,
            "raw_sha256": "0" * 64, "items": [], "extra": {"company": "A Bank", "role": "Filer", "filed_on": day},
            "event_at": None, "event_kind": None, "halt_code": None, "halt_time": None, "resumed_at": None}) + "\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path.stat().st_size


def test_repeated_calls_are_fast_on_10_mb_day_files(scout_dir: Path, cfg: ScoutConfig) -> None:
    """Owner, 2026-11-04: well under 0.1 s per call on 10 MB day files (0.2-0.8 s before the cache)."""
    _world(scout_dir, cfg)
    size = sum(_big_facts_day(scout_dir, d, 13_500) for d in ("2026-11-02", "2026-11-03", "2026-11-04"))
    assert size > 3 * 10_000_000
    reader = Reader(scout_dir)
    first = reader.entry_blocks(S, AS_OF)
    best = 1.0
    for i in range(5):
        t = time.perf_counter()
        res = reader.entry_blocks(S, AS_OF + timedelta(seconds=i))
        best = min(best, time.perf_counter() - t)
        assert res["value"] == first["value"] == "false"
    assert best < 0.1
