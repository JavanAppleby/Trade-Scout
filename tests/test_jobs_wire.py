"""scout.jobs_wire (DESIGN 2.2, 8): the jobs, wired through AppContext, end to end with a fake
transport: facts, health and raw files on disk, the status board and the heartbeat's gap flags."""

from __future__ import annotations

import logging
from datetime import timedelta

from helpers_fakes import FakeTransport
from helpers_wire import FORMS, full_transport, wire_config
from scout_reader.coverage import gaps
from scout_reader.records import read_jsonl

from scout.app import AppContext
from scout.config import config_from_dict
from scout.heartbeat import Heartbeat, SourceStatusBoard
from scout.http import HttpFetcher
from scout.jobs_wire import build_jobs
from scout.paths import ScoutPaths
from scout.scheduler import EveryJob, run_due

LOG = logging.getLogger("scout.test")


def context(cfg, clock, transport):
    ctx = AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, board=SourceStatusBoard(cfg), log=LOG)
    ctx._services["fetcher"] = HttpFetcher(cfg, clock, transport=transport)
    return ctx


def test_jobs_and_intervals(scout_dir, clock):
    cfg = wire_config(scout_dir)
    jobs = build_jobs(context(cfg, clock, FakeTransport()))
    assert [j.name for j in jobs] == ["edgar_tickers", "edgar_atom", "prnewswire", "globenewswire"]
    by = {j.name: j for j in jobs}
    assert all(isinstance(j, EveryJob) for j in jobs)
    assert by["edgar_atom"].interval_s == 60 and by["edgar_atom"].idle_interval_s == 600
    assert by["prnewswire"].interval_s == 60 and by["edgar_tickers"].interval_s == 60


def test_disabled_sources_get_no_job(scout_dir, clock):
    cfg = wire_config(scout_dir, sources={"globenewswire": {"enabled": False}, "edgar_atom": {"enabled": False}})
    ctx = context(cfg, clock, FakeTransport())
    assert [j.name for j in build_jobs(ctx)] == ["edgar_tickers", "prnewswire"]
    assert ctx.board.sources["globenewswire"].status == "disabled"


def test_end_to_end_polls_write_facts_health_and_raw(scout_dir, clock, write_audit):
    cfg = wire_config(scout_dir)
    transport = full_transport(cfg)
    ctx = context(cfg, clock, transport)
    jobs = build_jobs(ctx)
    heartbeat = Heartbeat(ctx.paths, cfg, ctx.board, clock)
    with write_audit.watching(ctx.paths.root):
        for _ in range(5):  # five ticks a minute apart
            run_due(jobs, clock.now(), LOG)
            heartbeat.beat(clock.now())
            clock.advance(60)
    facts = read_jsonl(ctx.paths.facts / "2026-11-02.jsonl")
    assert len({f["fact_id"] for f in facts}) == len(facts) == 9 + 21 + 3
    assert {f["source"] for f in facts} == {"edgar_atom", "prnewswire", "globenewswire"}
    acme = [f for f in facts if f["source"] == "edgar_atom" and f["ticker"] == "ACME"]
    assert acme and acme[0]["exchange"] == "Nasdaq"
    health = read_jsonl(ctx.paths.health / "2026-11-02.jsonl")
    assert {h["source"] for h in health} >= {"edgar_atom", "prnewswire", "globenewswire", "edgar_tickers"}
    assert len([h for h in health if h["source"] == "edgar_atom"]) == 5 * len(FORMS)
    assert any((ctx.paths.raw / "prnewswire").rglob("*.xml"))
    for name in ("edgar_atom", "prnewswire", "globenewswire", "edgar_tickers"):
        assert ctx.board.sources[name].status == "ok", name
    beats = read_jsonl(ctx.paths.beats / "2026-11-02.jsonl")
    assert beats[-1]["sources"]["prnewswire"] == "ok" and beats[-1]["gap_open"]["prnewswire"] is False
    assert beats[0]["gap_open"]["prnewswire"] is True  # the first full page: what came before is unknown
    # coverage from the stored records: covered after the first poll, a gap before it
    start = clock.now() - timedelta(minutes=4)
    for source in ("edgar_atom", "prnewswire"):
        assert gaps(health, source, start, clock.now() - timedelta(seconds=60), max_gap_s=cfg.source_max_gap_s) == []
    assert gaps(health, "prnewswire", start - timedelta(minutes=10), clock.now(),
                max_gap_s=cfg.source_max_gap_s)[0]["why"] == "no_poll_at_start"
    ua = [c for c in transport.calls if "User-Agent" in c[1]]
    assert len(ua) == len(transport.calls) > 0


def test_globenewswire_short_feed_overlaps_from_the_first_poll(scout_dir, clock):
    cfg = wire_config(scout_dir)
    ctx = context(cfg, clock, full_transport(cfg))
    jobs = build_jobs(ctx)
    run_due(jobs, clock.now(), LOG)
    health = read_jsonl(ctx.paths.health / "2026-11-02.jsonl")
    gnw = [h for h in health if h["source"] == "globenewswire"][0]
    assert gnw["items"] == 3 and gnw["overlap"] is True and gnw["gap_before"] is False


def test_no_contact_email_means_zero_transport_calls(scout_dir, clock, no_network):
    cfg = config_from_dict({"edgar_atom_forms": FORMS}, scout_dir)
    transport = full_transport(wire_config(scout_dir))
    ctx = context(cfg, clock, transport)
    jobs = build_jobs(ctx)
    for _ in range(3):
        run_due(jobs, clock.now(), LOG)
        clock.advance(60)
    assert transport.calls == [] and no_network.attempts == []
    assert not list(ctx.paths.facts.glob("*.jsonl"))
    assert ctx.board.sources["prnewswire"].status == "unavailable"
    assert ctx.board.sources["edgar_atom"].status == "stale"  # waits for the CIK map, which needs HTTP


def test_edgar_waits_for_the_ticker_map(scout_dir, clock):
    cfg = wire_config(scout_dir)
    transport = full_transport(cfg)
    del transport.responses[cfg.source("edgar_tickers").url]  # the map cannot be read
    ctx = context(cfg, clock, transport)
    run_due(build_jobs(ctx), clock.now(), LOG)
    assert not [c for c in transport.calls if "browse-edgar" in c[0]]
    assert ctx.board.sources["edgar_atom"].status == "stale" and ctx.board.sources["edgar_atom"].gap_open
