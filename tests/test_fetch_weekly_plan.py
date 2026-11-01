"""tools/fetch_weekly_plan.py: the weekly-plan PDF fetch the scheduled task runs (tasks/README.md).

Offline: the fetcher and the PDF text extractor are replaced by fakes; nothing touches the network.
"""

from __future__ import annotations

import importlib.util
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fetch_weekly_plan", REPO / "tools" / "fetch_weekly_plan.py")
fwp = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(fwp)

GOOD = "https://cdn.trendsetters.cloudflowventures.com/outward-weekly/2026-11-02/plan.pdf"
MONDAY = "2026-11-02"


def _pdf(_href: str) -> bytes:
    return b"%PDF-1.7 invented body"


def _pages(_body: bytes) -> list[str]:
    return ["Invented page one\nACME above 10\n", "", "Page three line\n  second line  "]


def test_good_url_passes() -> None:
    assert fwp.check_url(GOOD) == GOOD


@pytest.mark.parametrize("href", [
    "http://cdn.trendsetters.cloudflowventures.com/outward-weekly/x.pdf",          # not https
    "https://cdn.trendsetters.cloudflowventures.com.evil.example/outward-weekly/x",  # lookalike host
    "https://evil.example/outward-weekly/x.pdf",
    "https://user:pw@cdn.trendsetters.cloudflowventures.com/outward-weekly/x.pdf",  # credentials
    "https://cdn.trendsetters.cloudflowventures.com:8443/outward-weekly/x.pdf",     # odd port
    "https://cdn.trendsetters.cloudflowventures.com/other/x.pdf",                   # other path
    "file:///C:/x.pdf",
    "",
])
def test_bad_urls_refused(href: str) -> None:
    with pytest.raises(fwp.Refused):
        fwp.check_url(href)


def test_week_start_must_be_a_monday() -> None:
    assert fwp.check_week_start(MONDAY).isoformat() == MONDAY
    for bad in ("2026-11-03", "11/02/2026", "nonsense"):
        with pytest.raises(fwp.Refused):
            fwp.check_week_start(bad)


def test_success_writes_only_the_raw_text_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = fwp.run([GOOD, MONDAY], root=tmp_path, fetcher=_pdf, extractor=_pages)
    assert rc == 0
    out = tmp_path / "inbox" / "jason" / "raw" / f"{MONDAY}.weekly.txt"
    assert out.read_text(encoding="utf-8") == (
        "=== PAGE 1 ===\nInvented page one ACME above 10\n=== PAGE 2 ===\n\n"
        "=== PAGE 3 ===\nPage three line second line\n")
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [out]
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("OK: 3 pages")


def test_refusals_write_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cases = [
        ([GOOD.replace("https", "http"), MONDAY], _pdf, _pages),
        ([GOOD, "2026-11-04"], _pdf, _pages),
        ([GOOD, MONDAY], lambda _h: b"<html>not a pdf</html>", _pages),
        ([GOOD, MONDAY], lambda _h: b"%PDF-" + b"x" * fwp.MAX_BYTES, _pages),
        ([GOOD, MONDAY], _pdf, lambda _b: ["p"] * (fwp.MAX_PAGES + 1)),
        ([GOOD], _pdf, _pages),
    ]
    for argv, fetcher, extractor in cases:
        assert fwp.run(argv, root=tmp_path, fetcher=fetcher, extractor=extractor) == 2
        assert capsys.readouterr().out.strip().splitlines()[-1].startswith("REFUSED:")
    assert not any(tmp_path.rglob("*"))


def test_fetcher_refusal_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(_href: str) -> bytes:
        raise fwp.Refused("download failed (invented)")
    assert fwp.run([GOOD, MONDAY], root=tmp_path, fetcher=boom, extractor=_pages) == 2
    assert "download failed" in capsys.readouterr().out
    assert not any(tmp_path.rglob("*"))


def test_redirect_off_host_is_refused() -> None:
    handler = fwp._SameHostRedirects()
    req = urllib.request.Request(GOOD)
    with pytest.raises(fwp.Refused):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/outward-weekly/x.pdf")
    ok = handler.redirect_request(req, None, 302, "Found", {}, GOOD + "?v=2")
    assert ok is not None and ok.full_url == GOOD + "?v=2"


def test_damaged_pdf_is_refused_not_a_traceback() -> None:
    pytest.importorskip("pypdf")
    with pytest.raises(fwp.Refused):
        fwp.extract_pages(b"%PDF-1.7 not really a pdf")


def test_tool_imports_only_stdlib_at_top_level() -> None:
    import ast
    import sys
    tree = ast.parse((REPO / "tools" / "fetch_weekly_plan.py").read_text(encoding="utf-8"))
    tops = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            tops += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            tops.append(node.module.split(".")[0])
    assert all(t in sys.stdlib_module_names or t == "__future__" for t in tops)


@pytest.mark.parametrize("href", [GOOD.replace("plan.pdf", "a b.pdf"), GOOD + "\r\nX: y", GOOD.replace("plan", "pl\tan")])
def test_a_space_or_control_character_in_the_url_is_refused(href: str) -> None:
    with pytest.raises(fwp.Refused):
        fwp.check_url(href)
    handler = fwp._SameHostRedirects()
    with pytest.raises(fwp.Refused):
        handler.redirect_request(urllib.request.Request(GOOD), None, 302, "Found", {}, href)


@pytest.mark.parametrize("exc", [
    lambda: __import__("http.client").client.IncompleteRead(b"partial"),
    lambda: __import__("http.client").client.InvalidURL("invented"),
    lambda: __import__("http.client").client.RemoteDisconnected("invented"),
    lambda: ValueError("invented"),
])
def test_http_errors_become_refused_not_a_traceback(monkeypatch: pytest.MonkeyPatch, exc) -> None:
    class _Opener:
        def open(self, *_a, **_k):
            raise exc()

    monkeypatch.setattr(fwp.urllib.request, "build_opener", lambda *_a: _Opener())
    with pytest.raises(fwp.Refused, match="download failed"):
        fwp.fetch(GOOD)


def test_a_failed_write_is_refused_and_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    def no_rename(*_a, **_k):
        raise PermissionError("invented: file locked")

    monkeypatch.setattr(fwp.os, "replace", no_rename)
    assert fwp.run([GOOD, MONDAY], root=tmp_path, fetcher=_pdf, extractor=_pages) == 2
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("REFUSED: could not write")
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]
    # text the encoder cannot store (a lone surrogate from a damaged PDF) is refused the same way
    monkeypatch.undo()
    assert fwp.run([GOOD, MONDAY], root=tmp_path, fetcher=_pdf, extractor=lambda _b: ["x \udcff"]) == 2
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


def test_anything_unexpected_still_ends_with_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def odd(_href: str) -> bytes:
        raise RuntimeError("invented")
    assert fwp.run([GOOD, MONDAY], root=tmp_path, fetcher=odd, extractor=_pages) == 2
    assert capsys.readouterr().out.strip().splitlines()[-1] == "REFUSED: unexpected error (RuntimeError)"
