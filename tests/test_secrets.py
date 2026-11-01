"""Secrets (SCOUT_SPEC 2.8, 14 "a run's logs and files never contain the key pattern")."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from scout.app import AppContext, run
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.heartbeat import SourceStatusBoard
from scout.logs import close_logging, setup_logging
from scout.paths import ScoutPaths
from scout.secrets import load_env, redact

FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE_test-only"


def test_load_env_sets_only_allowed_names(paths: ScoutPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTHER_SECRET", raising=False)
    paths.env_file.write_text(f"# comment\nANTHROPIC_API_KEY=\"{FAKE_KEY}\"\nOTHER_SECRET=x\n", encoding="utf-8")
    assert load_env(paths) == ["ANTHROPIC_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE_KEY and "OTHER_SECRET" not in os.environ


def test_load_env_never_overrides(paths: ScoutPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-environment")
    paths.env_file.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    assert load_env(paths) == [] and os.environ["ANTHROPIC_API_KEY"] == "from-environment"


def test_load_env_missing_file(paths: ScoutPaths) -> None:
    assert load_env(paths) == []


def test_redact(cfg: ScoutConfig) -> None:
    text = f"key {FAKE_KEY} header x-api-key: abc123 mail {cfg.contact_email} and a@b.co"
    out = redact(text, cfg)
    assert "sk-ant-" not in out and "abc123" not in out and cfg.contact_email not in out and "a@b.co" not in out


def test_a_run_never_writes_the_key_or_email(paths: ScoutPaths, cfg: ScoutConfig, clock: ManualClock,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    paths.env_file.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    load_env(paths)
    log = setup_logging(paths, "DEBUG", clock.now().date(), cfg)
    assert "ANTHROPIC_LOG" not in os.environ
    try:
        log.info("careless message with %s and %s", FAKE_KEY, cfg.contact_email)
        log.error("another: %s", os.environ["ANTHROPIC_API_KEY"])
        ctx = AppContext(cfg=cfg, paths=paths, clock=clock, board=SourceStatusBoard(cfg), log=log)
        assert run(ctx, max_ticks=40, jobs=[]) == 0
    finally:
        close_logging()
    leaks = []
    for path in paths.root.rglob("*"):
        if path.is_file() and path != paths.env_file:
            data = path.read_bytes()
            if b"sk-ant-" in data or cfg.contact_email.encode() in data:
                leaks.append(path)
    assert leaks == []
    assert any(p.suffix == ".log" for p in paths.logs.iterdir())
    logging.getLogger("scout").handlers.clear()
