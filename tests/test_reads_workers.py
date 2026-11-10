"""The readers' own queues and worker pools (review-scout F1; owner's round 2, 2026-11-11).

Real threads with a fake transport that sleeps; Scout's clock runs `SCALE` times faster than the
wall clock, so "10 seconds a call" takes 10 / SCALE real seconds. Never the network, never a key.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from helpers_reads import DAY, answer, append_pack, pack, radar_dir, read_reply
from scout.clock import ManualClock
from scout.llm_client import execute
from scout.llm_prompts import LlmRequest
from scout.paths import ScoutPaths
from scout.reads_budget import ReadsBudget, reader_job
from scout.reads_file import reads_on
from scout.reads_service import ReadsService, selected
from scout.store import read_json

MADE = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)  # 09:30 ET, the open
SCALE = 50.0
SECONDS = {"claude-haiku-5-5": 10.0, "claude-opus-5-5": 60.0}  # Scout-clock seconds per call


class FastClock:
    """Scout's clock, running SCALE times faster than the wall clock."""

    def __init__(self, start: datetime) -> None:
        self.start, self.t0 = start, time.monotonic()

    def now(self) -> datetime:
        return self.start + timedelta(seconds=(time.monotonic() - self.t0) * SCALE)


class TimedTransport:
    """Thread-safe fake API: each call takes its model's time on Scout's clock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.active: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    def create(self, **params: Any) -> Any:
        model = params["model"]
        with self.lock:
            self.calls.append(model)
            self.active[model] = self.active.get(model, 0) + 1
            self.peak[model] = max(self.peak.get(model, 0), self.active[model])
        time.sleep(SECONDS[model] / SCALE)
        with self.lock:
            self.active[model] -= 1
        reread = "Look again" in json.dumps(params)
        return read_reply(answer(status="intact") if reread else None, model=model)


def _cfg(scout_dir: Path, *, r2: bool, **over: Any) -> Any:
    return make_config(scout_dir, radar_dir="../Radar", llm_enabled=True, reads_enabled=True,
                       llm={"readers": {"R2": {"enabled": r2}}}, **over)


def _wait(svc: ReadsService, paths: ScoutPaths, n_lines: int, timeout: float = 20.0) -> list[dict[str, Any]]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        lines = reads_on(paths, DAY)
        if svc.idle() and len(lines) >= n_lines:
            return lines
        time.sleep(0.01)
    raise AssertionError(f"reads not finished: {reads_on(paths, DAY)}")


def _age(line: dict[str, Any]) -> float:
    seen = datetime.fromisoformat(line["first_seen_at"].replace("Z", "+00:00"))
    return (seen - MADE).total_seconds()


def test_a_slow_r2_costs_r1_nothing(scout_dir: Path) -> None:
    radar = radar_dir(scout_dir)
    cfg = _cfg(scout_dir, r2=True)
    paths = ScoutPaths.from_config(cfg)
    r2 = cfg.reads.readers["R2"]
    cands = [c for c in range(1, 500) if selected(cfg, r2, DAY, c)][:6]  # every find in R2's sample too
    for c in cands:
        append_pack(radar, pack(symbol=f"S{c}", candidate_id=c, made_at=MADE))
    clock, tr = FastClock(MADE + timedelta(seconds=5)), TimedTransport()
    svc = ReadsService(cfg, paths, clock, tr, threaded=True)
    svc.tick(clock.now())
    lines = _wait(svc, paths, 12)
    r1 = [x for x in lines if x["reader"] == "R1"]
    r2_lines = [x for x in lines if x["reader"] == "R2"]
    assert [x["kind"] for x in r1] == ["first"] * 6 and [x["kind"] for x in r2_lines] == ["first"] * 6
    # R1: 6 calls of 10 s on 3 workers is two rounds; R2's 60 s calls run beside them, not in front
    assert max(_age(x) for x in r1) < 60 < min(_age(x) for x in r2_lines)
    assert tr.peak["claude-haiku-5-5"] <= 3 and tr.peak["claude-opus-5-5"] <= 3


def test_a_burst_of_twelve_finds_is_read_before_any_is_stale(scout_dir: Path) -> None:
    radar = radar_dir(scout_dir)
    cfg = _cfg(scout_dir, r2=False)
    assert cfg.reads.workers_per_reader == 3 and cfg.reads.max_pack_age_s == 120
    paths = ScoutPaths.from_config(cfg)
    made = MADE - timedelta(seconds=15)  # packed 15 s before Scout's poll saw them
    for c in range(1, 13):
        append_pack(radar, pack(symbol=f"S{c}", candidate_id=c, made_at=made))
    clock, tr = FastClock(MADE), TimedTransport()
    svc = ReadsService(cfg, paths, clock, tr, threaded=True)
    svc.tick(clock.now())
    lines = _wait(svc, paths, 12)
    assert [x["kind"] for x in lines] == ["first"] * 12  # no stale skip
    assert sorted(x["candidate_id"] for x in lines) == list(range(1, 13))
    assert tr.peak["claude-haiku-5-5"] == 3


def test_concurrent_reservations_cannot_cross_a_cap(scout_dir: Path, clock: ManualClock) -> None:
    req = LlmRequest(job=reader_job("R1"), model="claude-haiku-5-5", effort="low", max_tokens=1000,
                     system="s" * 350, user="u" * 350, schema={"type": "object"}, nonce="n")
    est = ReadsBudget(ScoutPaths.from_config(_cfg(scout_dir, r2=False)), _cfg(scout_dir, r2=False), clock).estimate(req)
    cfg = _cfg(scout_dir, r2=False, reads_daily_cost_cap_usd=round(est * 2.5, 8))
    paths = ScoutPaths.from_config(cfg)

    class SlowBudget(ReadsBudget):
        def _allow(self, job: str, request: LlmRequest) -> tuple[bool, str]:
            out = super()._allow(job, request)
            time.sleep(0.03)  # widen the gap between the check and the reservation
            return out

    budget = SlowBudget(paths, cfg, clock)
    release, lock = threading.Event(), threading.Lock()
    seen = {"sent": 0, "refused": 0}

    class Held:
        def create(self, **params: Any) -> Any:
            with lock:
                seen["sent"] += 1
            assert release.wait(10)
            return read_reply()

    start = threading.Barrier(6)

    def one() -> None:
        start.wait()
        out = execute(Held(), req, budget, budget.paths, clock)
        if out.status == "refused":
            with lock:
                seen["refused"] += 1

    threads = [threading.Thread(target=one, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    end = time.monotonic() + 10
    while seen["sent"] + seen["refused"] < 6 and time.monotonic() < end:
        time.sleep(0.01)
    release.set()
    for t in threads:
        t.join(10)
    assert (seen["sent"], seen["refused"]) == (2, 4)  # two reservations fit under the cap, never more


def test_reads_of_one_candidate_never_overlap_and_keep_their_order(scout_dir: Path) -> None:
    radar = radar_dir(scout_dir)
    cfg = _cfg(scout_dir, r2=False)
    paths = ScoutPaths.from_config(cfg)
    clock = ManualClock(MADE + timedelta(seconds=5))
    gate, order, lock = threading.Event(), [], threading.Lock()

    class Gated:
        def create(self, **params: Any) -> Any:
            text = json.dumps(params)
            reread = "Look again" in text
            symbol = "ABCD" if "Symbol ABCD" in text else "WXYZ"
            with lock:
                order.append((symbol, "reread" if reread else "first"))
            if symbol == "ABCD" and not reread:
                assert gate.wait(10)
            return read_reply(answer(status="intact") if reread else None)

    svc = ReadsService(cfg, paths, clock, Gated(), threaded=True)
    append_pack(radar, pack(symbol="ABCD", candidate_id=7, seq=0, made_at=MADE))
    t0 = time.monotonic()
    svc.tick(clock.now())
    append_pack(radar, pack(symbol="ABCD", candidate_id=7, seq=1, made_at=MADE))  # refresh while the first is out
    append_pack(radar, pack(symbol="WXYZ", candidate_id=8, seq=0, made_at=MADE))
    svc.tick(clock.now())
    assert time.monotonic() - t0 < 1.0  # the loop never waits for a call
    end = time.monotonic() + 10
    while len(reads_on(paths, DAY)) < 1 and time.monotonic() < end:
        time.sleep(0.01)
    (other,) = reads_on(paths, DAY)  # candidate 8 was read while 7's first read was still out ...
    assert other["candidate_id"] == 8 and order == [("ABCD", "first"), ("WXYZ", "first")]  # ... and 7:1 waited
    # the position is saved only up to the start of the unfinished first pack of 7
    assert read_json(paths.reads / "_tail.json")["files"][f"{DAY}.packs.jsonl"] == 0
    gate.set()
    lines = _wait(svc, paths, 3)
    seven = [x for x in lines if x["candidate_id"] == 7]
    assert [x["kind"] for x in seven] == ["first", "reread"] and seven[1]["parent_read_id"] == seven[0]["read_id"]
    size = (radar / "state" / "handoff" / "packs" / f"{DAY}.packs.jsonl").stat().st_size
    assert read_json(paths.reads / "_tail.json")["files"][f"{DAY}.packs.jsonl"] == size
    assert all(t.daemon for pool in svc._workers.values() for t in pool if t is not None)  # never holds up a stop


def test_one_first_read_per_reader_and_candidate(scout_dir: Path) -> None:
    radar = radar_dir(scout_dir)
    cfg = _cfg(scout_dir, r2=False)
    paths = ScoutPaths.from_config(cfg)
    clock = ManualClock(MADE + timedelta(seconds=5))
    tr = TimedTransport()
    svc = ReadsService(cfg, paths, clock, tr, threaded=False)
    append_pack(radar, pack(candidate_id=7, seq=0, made_at=MADE))
    append_pack(radar, pack(candidate_id=7, seq=0, made_at=MADE, pack_id=f"{DAY}:7:0b"))  # a second seq 0
    svc.tick(clock.now())
    assert [x["kind"] for x in reads_on(paths, DAY)] == ["first"] and len(tr.calls) == 1


class _Boom(BaseException):
    """Kills a worker thread (an Exception would be caught per pack)."""


def test_a_dead_worker_is_started_again(scout_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    radar = radar_dir(scout_dir)
    cfg = _cfg(scout_dir, r2=False, reads_workers_per_reader=1)
    paths = ScoutPaths.from_config(cfg)
    clock = ManualClock(MADE + timedelta(seconds=5))
    svc = ReadsService(cfg, paths, clock, TimedTransport(), threaded=True)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    real, calls = svc._handle, {"n": 0}

    def dies_once(reader: Any, p: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Boom()
        real(reader, p)

    monkeypatch.setattr(svc, "_handle", dies_once)
    append_pack(radar, pack(candidate_id=7, seq=0, made_at=MADE))
    svc.tick(clock.now())
    (worker,) = svc._workers["R1"]
    worker.join(5)
    assert not worker.is_alive() and svc.idle()
    append_pack(radar, pack(symbol="WXYZ", candidate_id=8, seq=0, made_at=MADE))
    svc.tick(clock.now())  # starts the worker again
    lines = _wait(svc, paths, 1)
    assert [x["candidate_id"] for x in lines] == [8]
    # the pack the dead worker had is not committed past: the next start polls it again
    assert read_json(paths.reads / "_tail.json")["files"][f"{DAY}.packs.jsonl"] == 0


def test_workers_per_reader_is_validated(scout_dir: Path) -> None:
    from scout.config_load import ConfigError  # noqa: PLC0415

    with pytest.raises(ConfigError, match="reads_workers_per_reader"):
        _cfg(scout_dir, r2=False, reads_workers_per_reader=0)
