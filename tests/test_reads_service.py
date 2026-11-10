"""The thesis readers (READS_TEST.md 5.1-5.4, 10). Mocked transport only: never the network."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from helpers_fake_anthropic import FakeAnthropic, message
from helpers_reads import DAY, answer, append_pack, pack, radar_dir, read_reply
from scout.clock import ManualClock
from scout.llm_ledger import Ledger
from scout.llm_records import calls_on
from scout.paths import ScoutPaths
from scout.reads_budget import reads_ledger_paths
from scout.reads_file import reads_on
from scout.store import read_jsonl
from scout.reads_service import ReadsService, sample_value, selected

MADE = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)  # 10:00 ET, CORE


def _cfg(scout_dir: Path, *, r2: bool = False, **over: Any):  # noqa: ANN202
    llm = {"readers": {"R2": {"enabled": r2}}}
    return make_config(scout_dir, radar_dir="../Radar", llm_enabled=True, reads_enabled=True, llm=llm, **over)


class Rig:
    def __init__(self, scout_dir: Path, *replies: Any, r2: bool = False, **over: Any) -> None:
        self.radar = radar_dir(scout_dir)
        self.cfg = _cfg(scout_dir, r2=r2, **over)
        self.paths = ScoutPaths.from_config(self.cfg)
        self.clock = ManualClock(MADE + timedelta(seconds=5))
        self.tr = FakeAnthropic(*replies)
        self.svc = self.service()

    def service(self) -> ReadsService:
        return ReadsService(self.cfg, self.paths, self.clock, self.tr, threaded=False)

    def feed(self, *packs: dict[str, Any]) -> list[dict[str, Any]]:
        for p in packs:
            append_pack(self.radar, p)
        self.svc.tick(self.clock.now())
        return reads_on(self.paths, DAY)

    def at(self, minutes: float) -> datetime:
        when = MADE + timedelta(minutes=minutes)
        self.clock.set(when + timedelta(seconds=5))
        return when


def test_sampling_is_repeatable_and_close_to_its_rate(scout_dir: Path) -> None:
    cfg = _cfg(scout_dir, r2=True)
    r1, r2 = cfg.reads.readers["R1"], cfg.reads.readers["R2"]
    picks = [selected(cfg, r2, DAY, c) for c in range(4000)]
    assert picks == [selected(cfg, r2, DAY, c) for c in range(4000)]
    assert 0.13 < sum(picks) / len(picks) < 0.17
    assert all(selected(cfg, r1, DAY, c) for c in range(500))  # sample 1.0 reads every find
    assert sample_value("reads-v1", DAY, 7, "R1") != sample_value("reads-v1", DAY, 7, "R2")
    assert sample_value("reads-v1", DAY, 7, "R2") != sample_value("reads-v1", "2026-11-03", 7, "R2")


def test_first_read_line_and_request(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply())
    lines = rig.feed(pack(seq=0, made_at=MADE))
    assert len(rig.tr.calls) == 1
    params = rig.tr.calls[0]
    assert params["model"] == "claude-haiku-5-5" and params["output_config"]["effort"] == "medium"
    assert params["output_config"]["format"]["schema"]["properties"]["status"]["enum"] == ["new"]
    assert "temperature" not in params and "thinking" not in params and "tools" not in params
    (line,) = lines
    assert line["kind"] == "first" and line["read_id"] == f"{DAY}:ABCD:1" and line["reader"] == "R1"
    assert (line["model"], line["effort"], line["prompt_version"]) == ("claude-haiku-5-5", "medium", "r1")
    assert (line["pack_id"], line["candidate_id"], line["session"]) == (f"{DAY}:7:0", 7, "CORE")
    assert (line["ref_bid"], line["ref_ask"]) == ("2.50", "2.52")
    assert (line["invalidation"], line["target"], line["status"]) == ("2.4", "2.8", "new")
    assert line["direction"] == "long" and line["strength"] == 4 and line["parent_read_id"] is None
    assert line["first_seen_at"].endswith("Z") and "." in line["first_seen_at"]
    assert isinstance(line["cost_usd"], str) and float(line["cost_usd"]) > 0 and line["usage"]["input_tokens"] == 3000
    assert line["cost_usd"] == "0.0006"  # 3000 in at $0.10 and 600 out at $0.50 a million: a decimal string
    # the call went through the ledger, under the reader's job name, reserved then settled
    recs = calls_on(reads_ledger_paths(rig.paths), MADE.date())
    assert [r["job"] for r in recs] == ["READ:R1"] and recs[0]["validation"] == "ok"
    assert Ledger(rig.paths, rig.cfg, rig.clock).spent_today() == 0.0


def test_wrong_side_prices_become_null(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(answer(invalidation=2.60, target=2.51)),
              read_reply(answer("short", 4, invalidation=2.40, target=2.30)))
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert line["invalidation"] is None and line["target"] is None
    (_, line2) = rig.feed(pack(seq=0, made_at=MADE, candidate_id=8, symbol="WXYZ"))
    assert line2["invalidation"] is None and line2["target"] == "2.3"


def test_reread_flow_and_thesis_ends_on_faded(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(), read_reply(answer(status="intact", strength=4)),
              read_reply(answer(status="faded", strength=3)))
    rig.feed(pack(seq=0, made_at=MADE))
    rig.feed(pack(seq=1, made_at=rig.at(5)))
    lines = rig.feed(pack(seq=2, made_at=rig.at(10)))
    assert [x["kind"] for x in lines] == ["first", "reread", "reread"]
    assert lines[1]["parent_read_id"] == lines[0]["read_id"] and lines[1]["status"] == "intact"
    assert lines[2]["read_id"] == f"{DAY}:ABCD:3"
    reread_params = rig.tr.calls[1]
    assert "FIRST" in reread_params["messages"][0]["content"]
    assert "new" not in reread_params["output_config"]["format"]["schema"]["properties"]["status"]["enum"]
    rig.feed(pack(seq=3, made_at=rig.at(15)))  # faded: no more rereads
    assert len(rig.tr.calls) == 3 and len(reads_on(rig.paths, DAY)) == 3


@pytest.mark.parametrize("second", [answer(status="weaker", strength=2), answer("short", 4, status="intact")])
def test_thesis_ends_on_strength_two_or_a_new_direction(scout_dir: Path, second: dict) -> None:
    rig = Rig(scout_dir, read_reply(), read_reply(second))
    rig.feed(pack(seq=0, made_at=MADE))
    rig.feed(pack(seq=1, made_at=rig.at(5)))
    rig.feed(pack(seq=2, made_at=rig.at(10)))
    assert len(rig.tr.calls) == 2


def test_thesis_ends_at_its_horizon(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(answer(horizon_min=5)))
    rig.feed(pack(seq=0, made_at=MADE))
    rig.feed(pack(seq=1, made_at=rig.at(6)))
    assert len(rig.tr.calls) == 1


def test_thesis_ends_after_the_most_rereads(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(), read_reply(answer(status="intact")), reads_reread_max=1)
    rig.feed(pack(seq=0, made_at=MADE))
    rig.feed(pack(seq=1, made_at=rig.at(5)))
    rig.feed(pack(seq=2, made_at=rig.at(10)))
    assert len(rig.tr.calls) == 2


def test_thesis_ends_with_its_session(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(answer(horizon_min=390)))
    rig.feed(pack(seq=0, made_at=MADE))
    rig.feed(pack(seq=1, made_at=rig.at(6 * 60 + 5), session="POST"))
    assert len(rig.tr.calls) == 1


def test_no_thesis_from_a_middling_or_flat_read_and_none_for_a_reader_without_rereads(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(answer(strength=3)), read_reply(answer("none", 4, invalidation=None,
                                                                          target=None)))
    rig.feed(pack(seq=0, made_at=MADE), pack(seq=0, made_at=MADE, candidate_id=9, symbol="WXYZ"))
    rig.feed(pack(seq=1, made_at=rig.at(5)), pack(seq=1, made_at=rig.at(5), candidate_id=9, symbol="WXYZ"))
    assert len(rig.tr.calls) == 2
    cfg = _cfg(scout_dir, r2=True)
    assert cfg.reads.readers["R2"].rereads is False and cfg.reads.readers["R2"].sample == 0.15


def test_a_stale_pack_gets_a_skip_line_and_no_call(scout_dir: Path) -> None:
    rig = Rig(scout_dir)
    rig.clock.set(MADE + timedelta(seconds=121))
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert (line["kind"], line["why"], line["reader"], line["pack_id"]) == ("skip", "stale", "R1", f"{DAY}:7:0")
    assert line["first_seen_at"] and rig.tr.calls == []


def test_a_pack_outside_the_sample_or_the_sessions_gets_no_line(scout_dir: Path) -> None:
    rig = Rig(scout_dir, reads_sessions=["PRE"])
    assert rig.feed(pack(seq=0, made_at=MADE)) == [] and rig.tr.calls == []
    cfg = _cfg(scout_dir, r2=True)
    unpicked = next(c for c in range(100, 400) if not selected(cfg, cfg.reads.readers["R2"], DAY, c))
    rig2 = Rig(scout_dir, read_reply(), r2=True)
    lines = rig2.feed(pack(seq=0, made_at=MADE, candidate_id=unpicked, symbol="QQQQ"))
    assert [x["reader"] for x in lines] == ["R1"]


def test_reads_daily_cap(scout_dir: Path) -> None:
    rig = Rig(scout_dir, reads_daily_cost_cap_usd=0.0001)
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert (line["kind"], line["why"]) == ("skip", "cap") and rig.tr.calls == []
    refused = calls_on(reads_ledger_paths(rig.paths), MADE.date())
    assert refused[0]["reason"] == "reads_daily_cap" and calls_on(rig.paths, MADE.date()) == []
    (_, again) = rig.feed(pack(seq=0, made_at=rig.at(1), candidate_id=8, symbol="WXYZ"))
    assert again["why"] == "cap" and rig.tr.calls == []
    # the refused reads never stop the other jobs
    assert Ledger(rig.paths, rig.cfg, rig.clock).cap_reached("J1") is False


def test_reads_monthly_cap(scout_dir: Path) -> None:
    from scout.models import LlmCall  # noqa: PLC0415
    from scout.llm_records import write_call  # noqa: PLC0415

    rig = Rig(scout_dir, reads_monthly_cost_cap_usd=2.0, reads_daily_cost_cap_usd=1.5)
    write_call(reads_ledger_paths(rig.paths), LlmCall(job="READ:R1", at="2026-11-01T15:00:00Z", model_id="claude-haiku-5-5",
                                  effort="medium", prompt_sha256="x", schema_version=1, request_id=None, usage={},
                                  cost_usd=1.9999, estimate_usd=1.9999, stop_reason="end_turn", validation="ok",
                                  retry=False, fact_ids=(), symbols=()))  # yesterday, same month
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert (line["why"], line["detail"]) == ("cap", "reads_monthly_cap") and rig.tr.calls == []


def test_reads_calls_are_recorded_in_their_own_ledger_folder(scout_dir: Path) -> None:
    from scout.models import LlmCall  # noqa: PLC0415
    from scout.llm_records import write_call  # noqa: PLC0415

    rig = Rig(scout_dir, read_reply(), llm_daily_cost_cap_usd=0.30)
    write_call(rig.paths, LlmCall(job="J1", at="2026-11-02T14:00:00Z", model_id="claude-haiku-5-5", effort="low",
                                  prompt_sha256="x", schema_version=1, request_id=None, usage={}, cost_usd=0.25,
                                  estimate_usd=0.25, stop_reason="end_turn", validation="ok", retry=False,
                                  fact_ids=(), symbols=()))
    main = Ledger(rig.paths, rig.cfg, rig.clock)
    before = (main.spent_today(), main.spent_month(), main.cap_reached("J1"), main.status())
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert line["kind"] == "first"
    reads = read_jsonl(rig.paths.reads / "ledger" / "2026-11-02.jsonl")
    assert [r["validation"] for r in reads] == ["reserved", "ok"] and all(r["job"] == "READ:R1" for r in reads)
    assert [r["job"] for r in calls_on(rig.paths, MADE.date())] == ["J1"]  # state/llm holds no reads record
    assert (main.spent_today(), main.spent_month(), main.cap_reached("J1"), main.status()) == before


def test_an_unreadable_ledger_is_an_error_skip_not_a_cap(scout_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scout.llm_ledger as L  # noqa: PLC0415

    def unreadable(_path: Any) -> list:
        raise L.LedgerUnreadable("PermissionError")

    monkeypatch.setattr(L, "_read", unreadable)
    rig = Rig(scout_dir)
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert (line["why"], line["detail"], rig.tr.calls) == ("error", "ledger_unreadable", [])


def test_a_reader_cap_stops_only_that_reader(scout_dir: Path) -> None:
    llm = {"readers": {"R2": {"enabled": True, "sample": 1.0, "daily_cost_cap_usd": 0.0001}}}
    rig = Rig(scout_dir, read_reply())
    rig.cfg = make_config(scout_dir, radar_dir="../Radar", llm_enabled=True, reads_enabled=True, llm=llm)
    rig.svc = rig.service()
    lines = rig.feed(pack(seq=0, made_at=MADE))
    by = {x["reader"]: x for x in lines}
    assert by["R1"]["kind"] == "first" and (by["R2"]["kind"], by["R2"]["why"]) == ("skip", "cap")
    assert len(rig.tr.calls) == 1 and rig.tr.calls[0]["model"] == "claude-haiku-5-5"


def test_other_jobs_at_their_cap_never_stop_a_read(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(), llm_daily_cost_cap_usd=0.0)
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert line["kind"] == "first"


def test_a_refusal_becomes_a_skip_line(scout_dir: Path) -> None:
    refuse = message({}, stop_reason="refusal")
    rig = Rig(scout_dir, refuse, refuse)
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert (line["kind"], line["why"], line["for"]) == ("skip", "refusal", "first")
    assert isinstance(line["cost_usd"], str) and float(line["cost_usd"]) > 0  # both attempts were billed


@pytest.mark.parametrize("bad, detail", [
    (answer(horizon_min=1000), "horizon_min out of range"),
    (answer(reasons=[]), "no reasons"),
])
def test_an_answer_failing_the_checks_is_invalid(scout_dir: Path, bad: dict, detail: str) -> None:
    rig = Rig(scout_dir, read_reply(bad))
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert (line["why"], line["detail"]) == ("invalid", detail)


def test_a_schema_failure_or_a_cut_off_is_invalid(scout_dir: Path) -> None:
    bad = read_reply({"direction": "up"})
    cut = message("{\"dir", stop_reason="max_tokens")
    rig = Rig(scout_dir, bad, bad, cut, cut)
    rig.feed(pack(seq=0, made_at=MADE))
    lines = rig.feed(pack(seq=0, made_at=MADE, candidate_id=8, symbol="WXYZ"))
    assert [(x["why"], x["detail"]) for x in lines] == [("invalid", "schema_fail"), ("invalid", "cut_off")]


def test_an_api_failure_is_an_error_skip(scout_dir: Path) -> None:
    rig = Rig(scout_dir, TimeoutError("boom"))
    (line,) = rig.feed(pack(seq=0, made_at=MADE))
    assert line["why"] == "error"


def test_a_restart_does_not_read_again(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(), read_reply(answer(status="intact")))
    rig.feed(pack(seq=0, made_at=MADE))
    rig.svc = rig.service()  # Scout restarted
    rig.svc.tick(rig.clock.now())
    assert len(rig.tr.calls) == 1 and len(reads_on(rig.paths, DAY)) == 1
    lines = rig.feed(pack(seq=1, made_at=rig.at(5)))  # the thesis survived the restart
    assert [x["kind"] for x in lines] == ["first", "reread"] and lines[1]["parent_read_id"] == lines[0]["read_id"]


def test_packs_read_but_not_handled_before_a_stop_are_handled_once_after(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply())
    append_pack(rig.radar, pack(seq=0, made_at=MADE))
    rig.svc.tailer.poll([MADE.date()], save=False)  # read, then Scout stopped before handling it
    rig.svc = rig.service()
    rig.svc.tick(rig.clock.now())
    rig.svc = rig.service()
    rig.svc.tick(rig.clock.now())
    assert len(rig.tr.calls) == 1 and len(reads_on(rig.paths, DAY)) == 1


def test_a_slow_api_never_holds_up_the_loop(scout_dir: Path) -> None:
    gate = threading.Event()

    def slow(_params: dict) -> Any:
        assert gate.wait(10)
        return read_reply()

    rig = Rig(scout_dir, slow)
    rig.svc = ReadsService(rig.cfg, rig.paths, rig.clock, rig.tr, threaded=True)
    append_pack(rig.radar, pack(seq=0, made_at=MADE))
    rig.svc.tick(rig.clock.now())  # returns at once while the call waits
    assert reads_on(rig.paths, DAY) == []
    gate.set()
    for _ in range(200):
        if rig.svc.idle() and reads_on(rig.paths, DAY):
            break
        threading.Event().wait(0.02)
    assert [x["kind"] for x in reads_on(rig.paths, DAY)] == ["first"]
