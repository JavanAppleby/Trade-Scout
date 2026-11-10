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
    (radar_dir / "state" / "handoff").mkdir(parents=True)
    (radar_dir / "state" / "handoff" / "2026-11-02.jsonl").write_text('{"format": 1, "symbol": "ABCD"}\n', encoding="utf-8")
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


# --------------------------------------------------------------------------- 0.3.2: Alpaca market data only

_ALPACA_HOST = re.compile(r"[\w.-]+\.alpaca\.markets", re.IGNORECASE)
_ALPACA_ANY = re.compile(r"alpaca\.markets", re.IGNORECASE)  # every mention, however the host is built
_DATA_HOST_TEXT = re.compile(r"(?<![\w.-])data\.alpaca\.markets(?!\.?[\w-])", re.IGNORECASE)
_TRADING_PATH = re.compile(r"/v2/(orders|account|positions|watchlists)|/v1/trading", re.IGNORECASE)


def _code_files(root: Path = REPO) -> list[Path]:
    files = [*(root / "scout").rglob("*.py"), *(root / "scout_reader").rglob("*.py"),
             *(p for pattern in ("*.bat", "*.ps1", "*.cmd") for p in root.glob(pattern))]
    tools = root / "tools"
    if tools.is_dir():
        files += [p for p in tools.rglob("*") if p.is_file() and p.suffix in (".py", ".bat", ".ps1", ".cmd")]
    return files


def _alpaca_problems(text: str) -> list[str]:
    """What the scan refuses in one file's text: an Alpaca host other than the data host (also a bare
    `alpaca.markets`, as in a host built from parts), or a broker path."""
    problems = [f"host {h}" for h in _ALPACA_HOST.findall(text) if h.lower() != "data.alpaca.markets"]
    if len(_ALPACA_ANY.findall(text)) != len(_DATA_HOST_TEXT.findall(text)):
        problems.append("alpaca.markets not written as data.alpaca.markets")
    problems += [f"path {m.group(0)}" for m in _TRADING_PATH.finditer(text)]
    return problems


def test_no_alpaca_trading_host_or_order_endpoint_in_code() -> None:
    """SS 2.1: Scout never trades. The only Alpaca host named anywhere in the code is the market-data
    host, and no order, account, position, watchlist or trading address appears at all."""
    named = 0
    for path in _code_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        assert _alpaca_problems(text) == [], path.name
        named += len(_DATA_HOST_TEXT.findall(text))
    assert named >= 1  # the pinned host is found, so the scan is looking at the right files


@pytest.mark.parametrize("text", [
    'ROOT = "alpaca.markets"; url = f"https://api.{ROOT}/v2/" + "orders"',
    'HOST = "alpaca.markets"', "https://paper-api.alpaca.markets/x", "https://data.alpaca.markets.evil.example/x",
    "https://xdata.alpaca.markets/x", 'path = "/v1/trading/accounts"', 'path = "/v2/watchlists"', '"/v2/orders"',
])
def test_the_scan_catches_a_host_built_from_parts_and_broker_paths(text: str) -> None:
    assert _alpaca_problems(text) != []


def test_the_scan_accepts_the_data_host_and_scans_root_scripts(tmp_path: Path) -> None:
    assert _alpaca_problems('DATA_HOST = "data.alpaca.markets"; f"https://{DATA_HOST}/v2/stocks/bars"') == []
    assert _alpaca_problems("Scout asks only data.alpaca.markets. Nothing else.") == []
    for name in ("Check.bat", "Run.ps1", "Go.cmd"):
        (tmp_path / name).write_text("curl https://api.alpaca.markets/v2/orders\n", encoding="utf-8")
    found = {p.name for p in _code_files(tmp_path)}
    assert found == {"Check.bat", "Run.ps1", "Go.cmd"}
    assert all(_alpaca_problems(p.read_text(encoding="utf-8")) for p in _code_files(tmp_path))


def test_market_host_is_the_data_host() -> None:
    from scout.alpaca_data import closes_url, delayed_url, live_url  # noqa: PLC0415
    from scout.config_reads import DATA_HOST, market_fetch_source  # noqa: PLC0415
    from scout.market_feed import SOURCE  # noqa: PLC0415

    assert DATA_HOST == "data.alpaca.markets" and SOURCE == "alpaca_market"
    cfg = config_from_dict({}, REPO)
    assert market_fetch_source(cfg).allowed_hosts == (DATA_HOST,)
    for url in (live_url(["SPY"]), delayed_url(["SPY"], START), closes_url(["SPY"], START.date(), START.date())):
        assert url.startswith(f"https://{DATA_HOST}/v2/stocks/")
