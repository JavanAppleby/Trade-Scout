"""Wiring check (DESIGN 8, 15 step 1): every job module in JOB_MODULES and every CLI handler exists.

Every unit has landed, so these checks are always strict (the SCOUT_STRICT_WIRING switch used while
the units were built in parallel is gone). `load_jobs(strict=True)` must build every job.
"""

from __future__ import annotations

import importlib
import logging

import pytest

from scout.app import JOB_MODULES, AppContext, load_jobs
from scout.cli import DISPATCH
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.heartbeat import SourceStatusBoard
from scout.paths import ScoutPaths


def test_job_modules_are_scout_modules_and_unique():
    assert len(JOB_MODULES) == len(set(JOB_MODULES))
    assert all(m.startswith("scout.jobs_") for m in JOB_MODULES)


def test_cli_handlers_follow_the_fixed_naming():
    for command, (module, attr) in DISPATCH.items():
        assert module.startswith("scout."), command
        assert attr.startswith("cli_"), command


@pytest.mark.parametrize("module_name", JOB_MODULES)
def test_job_module_imports_and_builds_jobs(module_name):
    module = importlib.import_module(module_name)
    build_jobs = getattr(module, "build_jobs", None)
    assert callable(build_jobs), f"{module_name} has no build_jobs(ctx) (scout.app.load_jobs calls it)"


@pytest.mark.parametrize("command", sorted(DISPATCH))
def test_cli_handler_exists(command):
    module_name, attr = DISPATCH[command]
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attr, None)), f"{module_name}.{attr} missing for '{command}'"


@pytest.mark.parametrize("llm_enabled", [False, True])
def test_every_job_builds_from_a_real_context(cfg: ScoutConfig, clock: ManualClock, llm_enabled: bool) -> None:
    """The real services (no network: nothing is fetched while building), strict loading."""
    from dataclasses import replace  # noqa: PLC0415

    c = replace(cfg, llm_enabled=llm_enabled)
    ctx = AppContext(cfg=c, paths=ScoutPaths.from_config(c), clock=clock, board=SourceStatusBoard(c),
                     log=logging.getLogger("test.wiring"))
    if llm_enabled:
        ctx._services["llm_transport"] = object()  # a stand-in; no call is made while building
    names = [j.name for j in load_jobs(ctx, strict=True)]
    assert len(names) == len(set(names))
    for expected in ("edgar_atom", "prnewswire", "nasdaq_halts", "bls", "cboe_vix", "fomc",
                     "jason_snapshot", "answers_pilot_1930", "answers_pilot_1945", "answers_radar", "brief", "verify"):
        assert expected in names, expected
    assert "globenewswire" not in names  # off by default since 2026-11-01 (it answered 403)
    assert ("llm_j1" in names) is llm_enabled and ("llm_j3" in names) is llm_enabled


def test_globenewswire_job_builds_when_turned_on(scout_dir, clock: ManualClock) -> None:
    from conftest import make_config  # noqa: PLC0415

    c = make_config(scout_dir, sources={"globenewswire": {"enabled": True}})
    ctx = AppContext(cfg=c, paths=ScoutPaths.from_config(c), clock=clock, board=SourceStatusBoard(c),
                     log=logging.getLogger("test.wiring"))
    names = [j.name for j in load_jobs(ctx, strict=True)]
    assert "globenewswire" in names and "prnewswire" in names
