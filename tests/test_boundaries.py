"""Scout's hard boundaries (SCOUT_SPEC 2.1, 2.2, 2.4, 6.1, 14 "Boundaries").

- No `webull*` or `trade-guardian` in Scout's dependencies, installed, or importable.
- No module imports webull, trade_guardian, radar or pilot; scout_reader imports the stdlib only;
  `anthropic` is imported only by scout.llm_client.
- A run writes only inside Scout's folder.
- An empty contact_email means no HTTP at all.
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

from scout.app import AppContext, run
from scout.clock import ManualClock
from scout.config import config_from_dict
from scout.heartbeat import SourceStatusBoard
from scout.paths import ScoutPaths

from conftest import START

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN_DIST = re.compile(r"^\s*(webull|trade[-_.]?guardian)", re.IGNORECASE)
FORBIDDEN_IMPORTS = ("webull", "trade_guardian", "radar", "pilot")


def _deps(pyproject: Path) -> list[str]:
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    deps = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        deps.extend(extra)
    return deps


def test_no_forbidden_dependency_declared() -> None:
    deps = _deps(REPO / "pyproject.toml")
    assert deps and not [d for d in deps if FORBIDDEN_DIST.match(d)]
    assert _deps(REPO / "scout_reader" / "pyproject.toml") == []  # the reader is stdlib only


def test_no_forbidden_package_installed_or_importable() -> None:
    names = {(d.metadata["Name"] or "") for d in md.distributions()}
    assert not [n for n in names if FORBIDDEN_DIST.match(n)]
    for module in ("webull", "trade_guardian"):
        assert importlib.util.find_spec(module) is None, f"{module} is importable from Scout's environment"


def _modules(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((a.name, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.module, node.lineno))
    return out


def test_no_forbidden_imports_anywhere() -> None:
    bad = []
    for folder in (REPO / "scout", REPO / "scout_reader", REPO / "tools"):
        for path in _modules(folder):
            for name, line in _imports(path):
                top = name.split(".")[0]
                if top in FORBIDDEN_IMPORTS and path.name != "make_calendar_fixture.py":
                    bad.append(f"{path.relative_to(REPO)}:{line} imports {name}")
    assert bad == []


def test_calendar_tool_imports_guardian_only_inside_its_function() -> None:
    # tools/make_calendar_fixture.py runs under Guardian's own Python, by hand; Scout never imports it.
    tree = ast.parse((REPO / "tools" / "make_calendar_fixture.py").read_text(encoding="utf-8"))
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not [n for n in top_level if "trade_guardian" in ast.dump(n)]


def test_scout_reader_is_stdlib_only() -> None:
    bad = []
    for path in _modules(REPO / "scout_reader" / "scout_reader"):
        for name, line in _imports(path):
            top = name.split(".")[0]
            if top not in sys.stdlib_module_names and top not in ("scout_reader", "__future__"):
                bad.append(f"{path.name}:{line} imports {name}")
    assert bad == []


def test_anthropic_only_in_llm_client() -> None:
    bad = []
    for path in _modules(REPO / "scout"):
        for name, line in _imports(path):
            if name.split(".")[0] == "anthropic" and path.name != "llm_client.py":
                bad.append(f"{path.name}:{line}")
    assert bad == []
    # importing Scout's core must not import anthropic (it is loaded only when the LLM is on)
    import subprocess  # noqa: PLC0415

    code = "import sys, scout.app, scout.cli, scout.config; print('anthropic' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO, check=True)
    assert out.stdout.strip() == "False"


def test_a_run_writes_only_inside_scouts_folder(tmp_path: Path, write_audit) -> None:  # noqa: ANN001
    scout_dir, radar_dir = tmp_path / "Scout", tmp_path / "Radar"
    scout_dir.mkdir()
    (radar_dir / "state" / "finds").mkdir(parents=True)
    (radar_dir / "state" / "finds" / "2026-11-02.jsonl").write_text('{"symbol": "ABCD"}\n', encoding="utf-8")
    before = sorted((p, p.stat().st_mtime_ns) for p in radar_dir.rglob("*"))
    cfg = config_from_dict({"radar_dir": str(radar_dir)}, scout_dir)
    paths = ScoutPaths.from_config(cfg)
    clock = ManualClock(START)
    import logging  # noqa: PLC0415

    ctx = AppContext(cfg=cfg, paths=paths, clock=clock, board=SourceStatusBoard(cfg), log=logging.getLogger("t"))
    with write_audit.watching(scout_dir):
        assert run(ctx, max_ticks=90) == 0
    assert write_audit.violations == []
    tops = {Path(p).relative_to(scout_dir.resolve()).parts[0] for _e, p in write_audit.events
            if Path(p) != scout_dir.resolve()}
    assert tops <= {"state", "logs", "reports", "inbox"}, tops
    assert sorted((p, p.stat().st_mtime_ns) for p in radar_dir.rglob("*")) == before


def test_empty_contact_email_means_no_http(scout_dir: Path, no_network) -> None:  # noqa: ANN001
    cfg = config_from_dict({}, scout_dir)
    assert cfg.http_allowed() is False and cfg.user_agent() == ""
    http = pytest.importorskip("scout.http", reason="scout.http is built by the wire unit")
    fetcher = http.HttpFetcher(cfg, ManualClock(START))
    result = fetcher.fetch("edgar_tickers", cfg.source("edgar_tickers").url)
    assert result.result == "no_http" and no_network.attempts == []


def test_webull_cannot_be_enabled(scout_dir: Path) -> None:
    from scout.config import ConfigError  # noqa: PLC0415

    with pytest.raises(ConfigError, match="scout_webull_enabled"):
        config_from_dict({"scout_webull_enabled": True}, scout_dir)
