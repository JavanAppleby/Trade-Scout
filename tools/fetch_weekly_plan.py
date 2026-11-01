"""Fetch the Paper Gains weekly-plan PDF and save its text into Scout's inbox (owner decision 2026-11-01).

    .venv\\Scripts\\python.exe tools\\fetch_weekly_plan.py <href> <week_start YYYY-MM-DD>

Run by the scheduled task `scout-jason-daily` (tasks/scout-jason-daily/SKILL.md), never by Scout
itself: the Scout program fetches none of Jason's material (SCOUT_SPEC 2 rule 10). The
#weekly-trade-plan "Weekly Watchlist" post carries the plan only behind its "CLICK HERE" link, a PDF
on the newsletter host. The owner approved fetching that newsletter, and only it.

What it does, and nothing else:
- refuses any URL that is not `https://cdn.trendsetters.cloudflowventures.com/outward-weekly/...`
  (exact host, no user/password, default port, no space or control character), and any redirect that
  leaves that rule;
- refuses a body over MAX_BYTES or one that is not a PDF (`%PDF-`), or with more than MAX_PAGES pages;
- writes the text, one "=== PAGE n ===" block per page, to
  `<scout folder>\\inbox\\jason\\raw\\<week_start>.weekly.txt` (the folder holding this tools\\ folder),
  through a temp file and a rename. It writes no other file, reads no secret, places no order.

The text is untrusted third-party data for the task to transcribe; Scout never reads `raw\\`.
Needs `pypdf` (the optional extra: `.venv\\Scripts\\python.exe -m pip install -e ".[weekly]"`).
Exit 0 with a last line "OK: ..."; exit 2 with a last line "REFUSED: <reason>" -- for every failure,
a network or HTTP error, a failed write (no temp file is left) or anything unexpected; never a traceback.
"""

from __future__ import annotations

import http.client
import io
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_HOST = "cdn.trendsetters.cloudflowventures.com"
ALLOWED_PATH_PREFIX = "/outward-weekly/"
MAX_BYTES = 30 * 1024 * 1024
MAX_PAGES = 200
TIMEOUT_S = 60
RAW_SUBDIR = ("inbox", "jason", "raw")


class Refused(Exception):
    """A reason to stop without writing anything."""


def check_url(href: str) -> str:
    """The URL if it is on the approved host and path over https; else raise Refused."""
    if not isinstance(href, str) or not href.strip():
        raise Refused("no URL given")
    href = href.strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in href):
        raise Refused("a URL with a space or control character is not allowed")
    try:
        parts = urlsplit(href)
        port = parts.port
    except ValueError as exc:
        raise Refused(f"not a valid URL ({exc})") from None
    if parts.scheme != "https":
        raise Refused("only https is allowed")
    if parts.username is not None or parts.password is not None:
        raise Refused("a URL with a user name or password is not allowed")
    if (parts.hostname or "").lower() != ALLOWED_HOST:
        raise Refused(f"only {ALLOWED_HOST} is approved for fetching, not {parts.hostname!r}")
    if port not in (None, 443):
        raise Refused(f"port {port} is not allowed")
    if not parts.path.startswith(ALLOWED_PATH_PREFIX):
        raise Refused(f"the path must start with {ALLOWED_PATH_PREFIX}")
    return href


def check_week_start(text: str) -> date:
    try:
        day = date.fromisoformat(text)
    except (TypeError, ValueError):
        raise Refused(f"week_start must be YYYY-MM-DD, got {text!r}") from None
    if day.weekday() != 0:
        raise Refused(f"week_start must be a Monday, {day.isoformat()} is a {day.strftime('%A')}")
    return day


def output_path(root: Path, week_start: date) -> Path:
    return Path(root).joinpath(*RAW_SUBDIR) / f"{week_start.isoformat()}.weekly.txt"


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        check_url(newurl)  # raises Refused for any redirect off the approved host/path
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(href: str, max_bytes: int = MAX_BYTES) -> bytes:
    """GET the PDF (no cookies, no auth); refuse a body over max_bytes."""
    opener = urllib.request.build_opener(_SameHostRedirects())
    req = urllib.request.Request(href, headers={"User-Agent": "Mozilla/5.0 (Scout weekly-plan fetch)"})
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            check_url(resp.geturl())
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise Refused(f"the PDF is {int(length)} bytes, over the {max_bytes}-byte cap")
            body = resp.read(max_bytes + 1)
    except (urllib.error.URLError, OSError) as exc:  # URLError is an OSError; timeouts too
        raise Refused(f"download failed ({exc})") from None
    except (http.client.HTTPException, ValueError) as exc:  # IncompleteRead (chunked), InvalidURL, ...
        raise Refused(f"download failed ({type(exc).__name__})") from None
    if len(body) > max_bytes:
        raise Refused(f"the PDF is over the {max_bytes}-byte cap")
    return body


def extract_pages(body: bytes) -> list[str]:
    """One string per page (pypdf)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise Refused('pypdf is not installed; the owner runs once: .venv\\Scripts\\python.exe -m pip install -e ".[weekly]"') from None
    try:
        reader = PdfReader(io.BytesIO(body))
        if len(reader.pages) > MAX_PAGES:
            raise Refused(f"the PDF has {len(reader.pages)} pages, over the {MAX_PAGES}-page cap")
        return [page.extract_text() or "" for page in reader.pages]
    except Refused:
        raise
    except Exception as exc:  # a damaged or unexpected PDF: stop, write nothing
        raise Refused(f"the PDF could not be read ({type(exc).__name__}: {exc})") from None


def to_text(pages: list[str]) -> str:
    blocks = []
    for i, page in enumerate(pages, 1):
        words = [w.strip() for w in page.split("\n") if w.strip()]
        blocks.append(f"=== PAGE {i} ===\n" + " ".join(words))
    return "\n".join(blocks) + "\n"


def _write(out: Path, text: str) -> None:
    """The text file through a temp file and a rename; on any failure no temp file is left behind."""
    tmp = out.with_name(out.name + ".tmp")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)
    except (OSError, ValueError) as exc:  # disk full, locked, or text that cannot be encoded
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        raise Refused(f"could not write {out.name} ({type(exc).__name__})") from None


def run(argv: list[str], *, root: Path | None = None,
        fetcher: Callable[[str], bytes] = fetch,
        extractor: Callable[[bytes], list[str]] = extract_pages) -> int:
    if len(argv) != 2:
        print(__doc__)
        print("REFUSED: usage: fetch_weekly_plan.py <href> <week_start YYYY-MM-DD>")
        return 2
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    try:
        href = check_url(argv[0])
        week = check_week_start(argv[1])
        body = fetcher(href)
        if len(body) > MAX_BYTES:
            raise Refused(f"the PDF is over the {MAX_BYTES}-byte cap")
        if not body.startswith(b"%PDF-"):
            raise Refused("the download is not a PDF")
        pages = extractor(body)
        if len(pages) > MAX_PAGES:
            raise Refused(f"the PDF has {len(pages)} pages, over the {MAX_PAGES}-page cap")
        out = output_path(root, week)
        _write(out, to_text(pages))
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return 2
    except Exception as exc:  # the contract is a last line OK or REFUSED, never a traceback
        print(f"REFUSED: unexpected error ({type(exc).__name__})")
        return 2
    print(f"OK: {len(pages)} pages, {len(body)} bytes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
