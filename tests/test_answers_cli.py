"""`python -m scout answers` (DESIGN 2.5, 8): prints the reader's answers and writes nothing."""

from __future__ import annotations

import argparse
import io
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scout import cli
from scout.answers_cli import cli_answers, parse_as_of
from scout.app import AppContext
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.heartbeat import SourceStatusBoard
from scout.paths import ScoutPaths

from conftest import TEST_EMAIL, et_at
from helpers_answers import due_record, fact, good_world, install_all_stubs
from helpers_fakes import write_records

AS_OF = et_at("2026-11-03", "19:45")


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    install_all_stubs(monkeypatch)


def _ctx(cfg: ScoutConfig) -> AppContext:
    return AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=ManualClock(AS_OF),
                      board=SourceStatusBoard(cfg), log=logging.getLogger("test.answers_cli"))


def _run(ctx: AppContext, *symbols: str, as_of: str | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = cli_answers(ctx, argparse.Namespace(symbols=list(symbols), as_of=as_of), out=out)
    return code, out.getvalue()


def test_prints_every_answer_and_writes_nothing(scout_dir: Path, cfg: ScoutConfig, write_audit: Any) -> None:
    good_world(scout_dir, cfg, AS_OF)
    write_records(scout_dir, "facts", [fact("ACME", published=AS_OF - timedelta(minutes=30), cls="fda")])
    write_records(scout_dir, "due", [due_record("ACME", AS_OF - timedelta(minutes=1), due_start="2027-01-10",
                                                due_end="2027-04-05")])
    before = sorted(p.relative_to(scout_dir) for p in scout_dir.rglob("*"))
    write_audit.start(scout_dir)
    code, text = _run(_ctx(cfg), "acme", "beta")
    assert code == 0 and not write_audit.events  # not a single write, even inside Scout's folder
    assert sorted(p.relative_to(scout_dir) for p in scout_dir.rglob("*")) == before
    assert "ACME:" in text and "BETA:" in text and "Market:" in text
    acme = text.split("ACME:")[1].split("BETA:")[0]
    assert re.search(r"fresh_catalyst \(gate 6\)\s+true", acme)
    assert re.search(r"event_before_next_session\s+false", acme)
    assert re.search(r"allow a hold\s+yes", acme)
    beta = text.split("BETA:")[1]
    assert "no_due_record" in beta and re.search(r"allow a hold\s+no", beta)
    assert "Scout never trades" in text and "ET /" in text and TEST_EMAIL not in text


def test_as_of_option(scout_dir: Path, cfg: ScoutConfig) -> None:
    good_world(scout_dir, cfg, AS_OF)
    code, text = _run(_ctx(cfg), "ACME", as_of="2026-11-04T00:45:00Z")
    assert code == 0 and "as of 2026-11-04T00:45:00Z" in text
    code, text = _run(_ctx(cfg), "ACME", as_of="2026-11-04T10:45:00+10:00")
    assert code == 0 and "as of 2026-11-04T00:45:00Z" in text
    code, text = _run(_ctx(cfg), "ACME", as_of="2026-11-04T09:00:00Z")  # later: Scout is stale then
    assert code == 0 and "[stale]" in text


@pytest.mark.parametrize("value", ["2026-11-04T00:45:00", "yesterday", "2026-13-01T00:00:00Z"])
def test_bad_as_of_is_exit_2(scout_dir: Path, cfg: ScoutConfig, value: str) -> None:
    code, text = _run(_ctx(cfg), "ACME", as_of=value)
    assert code == 2 and "--as-of" in text


def test_no_symbols_is_exit_2(cfg: ScoutConfig) -> None:
    assert _run(_ctx(cfg), " ")[0] == 2


def test_parse_as_of_defaults_to_now() -> None:
    assert parse_as_of(None, AS_OF) == AS_OF and parse_as_of("  ", AS_OF) == AS_OF
    assert parse_as_of("2026-11-04T00:45:00z", AS_OF) == AS_OF


def test_through_the_cli(scout_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = scout_dir / "config.toml"
    config.write_text(f'contact_email = "{TEST_EMAIL}"\n', encoding="utf-8")
    assert cli.main(["--config", str(config), "answers", "ACME", "--as-of", "2026-11-04T00:45:00Z"]) == 0
    out = capsys.readouterr().out
    assert "ACME:" in out and "scout_stale" in out
    assert cli.main(["--config", str(config), "answers", "ACME", "--as-of", "naive"]) == 2
