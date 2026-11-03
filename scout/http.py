"""The single fetch path (SCOUT_SPEC 6.1, 12; DESIGN 2.2 and 10).

`HttpFetcher.fetch(source, url)` never raises: every outcome is a `FetchResult` whose `result` is
one of `models.HEALTH_RESULTS`. In order:

1. An empty `contact_email` means no HTTP at all: `no_http`, and no socket is opened.
2. The scheme must be https and the host must be in the source's `allowed_hosts`: else `refused`.
   An unknown or disabled source is refused too.
3. A host stops at its daily cap for the ET day: `host_cap`. The cap is the smaller of
   `host_max_requests_per_day` (exact host or its hard-capped domain) and the hard cap in code
   (`http_state.HARD_MAX_REQUESTS_PER_DAY`: nowflation.com 10). A hard cap covers every host under
   its domain, which share one count (www.nowflation.com counts as nowflation.com).
4. A host in backoff (after a 403 or 429) is not asked at all until the backoff has passed:
   `backoff`. Otherwise the fetcher waits (the clock's `sleep`) until the host's spacing has passed
   since its last request. Spacing = max(`host_min_spacing_s[host]` or
   `http_default_host_spacing_s`, the hard minimum in code). Hard minimums: www.nasdaqtrader.com
   60 s (its feed asks for at most one read a minute), www.sec.gov and data.sec.gov 0.5 s (SS 12:
   EDGAR at most 2 requests a second).
5. Headers: the declared User-Agent (`cfg.user_agent()`), `Accept-Encoding: gzip, deflate`, and for
   a conditional fetch the ETag / Last-Modified last seen for that URL (memory only).
6. A body over `fetch_max_bytes` (before or after decompression) is `too_large`.
7. 200 -> `ok`; 304 -> `not_modified`; 403/429 -> `http_error` and the host's backoff doubles
   (starting from twice its spacing) up to `source_backoff_max_min`; the next 200 or 304 resets it.
   Other statuses -> `http_error`; a timeout -> `timeout`; any other failure -> `network`. There is
   no automatic retry and no redirect is followed (a 3xx is an `http_error`).
8. Logs carry source, host, result, status, bytes and milliseconds only: never a header, the
   User-Agent or the contact email.
9. Given a state file, the per-host day count, last request time and backoff, and the last request
   time per source, are kept in `state/http_hosts.json` (`scout.http_state`), so a restart never
   resets a cap, a spacing or a backoff.
10. TLS certificates are verified against the operating system's trust store through `truststore`
   (pip's own approach): on Windows, Python's OpenSSL with the Windows store picked an expired path
   for the Let's Encrypt chains of www.globenewswire.com and cdn.cboe.com (2026-10-31) while Windows
   itself accepted them (the VIX file has since moved to cdn-api.cboe.com: cdn.cboe.com answers 307
   there, and no redirect is followed). Hostname checking and CERT_REQUIRED are always on; verification is never
   turned off. Without `truststore` importable, Python's default context is used (`TLS_TRUST` says
   which; `build_fetcher` logs it).
"""

from __future__ import annotations

import logging
import socket
import ssl
import time as _time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from scout.clock import Clock, et_date, iso
from scout.config import ScoutConfig
from scout.http_state import FILE_NAME, HostLimits, HostStateFile, cap_key, hard_cap
from scout.models import FetchResult

__all__ = ["HARD_MIN_SPACING_S", "TLS_TRUST", "HttpFetcher", "RawResponse", "Transport", "build_fetcher",
           "decompress", "ssl_context", "urllib_transport"]

# Per-host minimums no config can lower (SS 12: "enforced per host in code"): Nasdaq Trader one read a
# minute, SEC fair access, and the RSS wires at least 30 s apart.
HARD_MIN_SPACING_S: Mapping[str, float] = {"www.nasdaqtrader.com": 60.0, "www.sec.gov": 0.5, "data.sec.gov": 0.5,
                                           "www.prnewswire.com": 30.0}
_BACKOFF_STATUSES = (403, 429)


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


Transport = Callable[[str, Mapping[str, str], float, int], Any]  # returns a RawResponse-like object


class HostBlocked(Exception):
    """Internal: a request the fetcher will not send (carries the FetchResult `result`)."""

    def __init__(self, result: str, message: str) -> None:
        super().__init__(message)
        self.result = result


class TooLarge(Exception):
    """Internal: a body over the cap."""


# --------------------------------------------------------------------------- transport


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could lead off the allowed hosts: never follow one (it comes back as a 3xx)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def ssl_context() -> tuple[ssl.SSLContext, str]:
    """(context, which): the OS trust store through `truststore` ("os_trust_store"), else Python's
    default context ("python_default"). Either way CERT_REQUIRED with hostname checking."""
    try:
        import truststore  # noqa: PLC0415 - optional at import: the fallback below covers its absence
    except ImportError:
        ctx, which = ssl.create_default_context(), "python_default"
    else:
        ctx, which = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "os_trust_store"
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx, which


_SSL_CONTEXT, TLS_TRUST = ssl_context()
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_SSL_CONTEXT))


def urllib_transport(url: str, headers: Mapping[str, str], timeout_s: float, max_bytes: int) -> RawResponse:
    """The only real transport. Reads at most `max_bytes`; an HTTP error comes back as its status;
    timeouts and network failures raise (HttpFetcher maps them)."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with _OPENER.open(request, timeout=timeout_s) as resp:  # noqa: S310 - https, allowed hosts only
            return RawResponse(int(resp.status), {k.lower(): v for k, v in resp.headers.items()},
                               resp.read(max_bytes))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(max_bytes) or b""
        except Exception:  # noqa: BLE001 - the status is what matters
            body = b""
        hdrs = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return RawResponse(int(exc.code), hdrs, body)


def decompress(raw: bytes, encoding: str, limit: int) -> bytes:
    """gzip / deflate decoding (Radar's `_decompress`, catalysts_edgar.py at commit 6c971c8), with
    the output capped at `limit` bytes (a decompression bomb is `too_large`)."""
    enc = (encoding or "").strip().lower()
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        return _inflate(raw, 16 + zlib.MAX_WBITS, limit)
    if enc == "deflate":
        try:
            return _inflate(raw, zlib.MAX_WBITS, limit)
        except zlib.error:
            return _inflate(raw, -zlib.MAX_WBITS, limit)  # raw deflate stream
    return raw


def _inflate(raw: bytes, wbits: int, limit: int) -> bytes:
    d = zlib.decompressobj(wbits)
    out = d.decompress(raw, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise TooLarge("decompressed body over the cap")
    return out + d.flush()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lower = name.lower()
    for k, v in headers.items():
        if str(k).lower() == lower:
            return str(v)
    return None


# --------------------------------------------------------------------------- the fetcher


class HttpFetcher:
    """implements interfaces.Fetcher. One instance per Scout run (state is per host; kept in a
    state file across restarts when `state` is given)."""

    def __init__(self, cfg: ScoutConfig, clock: Clock, *, transport: Transport | None = None,
                 log: logging.Logger | None = None, state: HostStateFile | None = None) -> None:
        self.cfg = cfg
        self.clock = clock
        self.transport: Transport = transport or urllib_transport
        self.log = log or logging.getLogger("scout.http")
        self._state = state
        self._limits = state.load() if state is not None else HostLimits()
        self._last_request: dict[str, datetime] = self._limits.last_request
        self._backoff_s: dict[str, float] = self._limits.backoff_s
        self._blocked_until: dict[str, datetime] = self._limits.blocked_until
        self._day_count: dict[str, tuple[date, int]] = self._limits.day_count
        self._source_last: dict[str, datetime] = self._limits.source_last_request
        self._clamp_future(self.clock.now())
        self._validators: dict[str, tuple[str | None, str | None]] = {}  # url -> (etag, last-modified)
        self.requests = 0

    def _clamp_future(self, now: datetime) -> None:
        """Times stored in the future (the wall clock stepped back since they were saved) would make
        a fetch sleep for hours: a last request is clamped to now, a backoff to its longest length."""
        for times in (self._last_request, self._source_last):
            for key, at in list(times.items()):
                if at > now:
                    times[key] = now
        longest = now + timedelta(minutes=float(self.cfg.source_backoff_max_min))
        for host, until in list(self._blocked_until.items()):
            if until > longest:
                self._blocked_until[host] = longest

    def __repr__(self) -> str:  # never show the User-Agent or contact email
        return f"HttpFetcher(requests={self.requests})"

    # ------------------------------------------------------------------ policy

    def spacing_for(self, host: str) -> float:
        configured = self.cfg.host_min_spacing_s.get(host, self.cfg.http_default_host_spacing_s)
        return max(float(configured), HARD_MIN_SPACING_S.get(host, 0.0))

    def backoff_for(self, host: str) -> float:
        return self._backoff_s.get(host, 0.0)

    def requests_today(self, host: str, now: datetime) -> int:
        day, n = self._day_count.get(cap_key(host), (None, 0))
        return n if day == et_date(now) else 0

    def cap_for(self, host: str) -> int | None:
        """The daily cap for `host`: min(config, hard cap in code); None when there is none."""
        caps = self.cfg.host_max_requests_per_day
        h = host.lower()
        configured = caps.get(h, caps.get(cap_key(h)))
        found = [int(c) for c in (configured, hard_cap(h)) if c is not None]
        return min(found) if found else None

    def last_request_at(self, host: str) -> datetime | None:
        """When the last request to `host` was sent (kept across restarts)."""
        return self._last_request.get(host.lower())

    def source_last_request_at(self, source: str) -> datetime | None:
        """When the last request for `source` was sent (kept across restarts)."""
        return self._source_last.get(source)

    def _save(self) -> None:
        if self._state is not None:
            self._state.save(self._limits)

    def _check(self, source: str, url: str) -> str:
        """The host, or HostBlocked."""
        if not self.cfg.http_allowed():
            raise HostBlocked("no_http", "contact_email is empty: no HTTP")
        try:
            src = self.cfg.source(source)
        except KeyError:
            raise HostBlocked("refused", f"unknown source {source!r}") from None
        if not src.enabled:
            raise HostBlocked("refused", f"source {source} is disabled")
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https":
            raise HostBlocked("refused", "only https is allowed")
        if host not in {h.lower() for h in src.allowed_hosts}:
            raise HostBlocked("refused", f"host {host or '?'} is not allowed for {source}")
        if parts.username or parts.password or (parts.port not in (None, 443)):
            raise HostBlocked("refused", "credentials or a non-standard port in the URL")
        now = self.clock.now()
        cap = self.cap_for(host)
        if cap is not None and self.requests_today(host, now) >= cap:
            raise HostBlocked("host_cap", f"{host}: {cap} requests today already")
        until = self._blocked_until.get(host)
        if until is not None and now < until:
            raise HostBlocked("backoff", f"{host}: backing off until {iso(until)}")
        return host

    def _wait_spacing(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is None:
            return
        spacing = self.spacing_for(host)
        elapsed = (self.clock.now() - last).total_seconds()
        if elapsed < 0:  # the wall clock stepped back: the spacing counts as passed, never a long sleep
            return
        wait = min(spacing, spacing - elapsed)
        if wait > 0:
            sleep = getattr(self.clock, "sleep", None)
            (sleep or _time.sleep)(wait)

    def _note_request(self, host: str, now: datetime, source: str | None = None) -> None:
        self._last_request[host] = now
        if source is not None:
            self._source_last[source] = now
        day = et_date(now)
        key = cap_key(host)
        prev_day, n = self._day_count.get(key, (day, 0))
        self._day_count[key] = (day, (n if prev_day == day else 0) + 1)
        self.requests += 1
        self._save()

    def _back_off(self, host: str, source: str) -> float:
        src_spacing = 0.0
        try:
            src_spacing = self.cfg.source(source).min_spacing_s
        except KeyError:  # pragma: no cover - checked before
            pass
        base = max(self.spacing_for(host), src_spacing, 1.0)
        cap = self.cfg.source_backoff_max_min * 60.0
        current = self._backoff_s.get(host, 0.0)
        new = min(cap, max(2.0 * base, 2.0 * current))
        self._backoff_s[host] = new
        self._blocked_until[host] = self.clock.now() + timedelta(seconds=new)
        self._save()
        return new

    def _reset_backoff(self, host: str) -> None:
        if host in self._backoff_s or host in self._blocked_until:
            self._backoff_s.pop(host, None)
            self._blocked_until.pop(host, None)
            self._save()

    # ------------------------------------------------------------------ fetch

    def fetch(self, source: str, url: str, *, conditional: bool = True,
              max_bytes: int | None = None) -> FetchResult:
        started = self.clock.now()
        try:
            return self._fetch(source, url, conditional, max_bytes, started)
        except HostBlocked as exc:
            return self._result(source, url, exc.result, started, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - fetch never raises
            return self._result(source, url, "network", started, error=f"unexpected {type(exc).__name__}")

    def _fetch(self, source: str, url: str, conditional: bool, max_bytes: int | None,
               started: datetime) -> FetchResult:
        host = self._check(source, url)
        cap = int(max_bytes) if max_bytes is not None else int(self.cfg.fetch_max_bytes)
        cap = max(1, min(cap, int(self.cfg.fetch_max_bytes)))
        self._wait_spacing(host)
        headers = {"User-Agent": self.cfg.user_agent(), "Accept-Encoding": "gzip, deflate"}
        if conditional and url in self._validators:
            etag, modified = self._validators[url]
            if etag:
                headers["If-None-Match"] = etag
            if modified:
                headers["If-Modified-Since"] = modified
        sent_at = self.clock.now()
        self._note_request(host, sent_at, source)
        t0 = _time.perf_counter()
        try:
            resp = self.transport(url, headers, float(self.cfg.fetch_timeout_s), cap + 1)
        except (TimeoutError, socket.timeout) as exc:
            return self._done(source, url, host, "timeout", sent_at, t0, error=type(exc).__name__)
        except urllib.error.URLError as exc:
            timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
            return self._done(source, url, host, "timeout" if timed_out else "network", sent_at, t0,
                              error=type(exc).__name__)
        except OSError as exc:
            return self._done(source, url, host, "network", sent_at, t0, error=type(exc).__name__)
        status = int(getattr(resp, "status", 0) or 0)
        resp_headers = {str(k).lower(): str(v) for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
        raw = bytes(getattr(resp, "body", b"") or b"")
        if status in _BACKOFF_STATUSES:
            wait = self._back_off(host, source)
            self.log.warning("%s: %s answered %s; backing off %.0f s", source, host, status, wait)
            return self._done(source, url, host, "http_error", sent_at, t0, status=status, headers=resp_headers,
                              error=f"HTTP {status}; backoff {wait:.0f} s")
        if status == 304:
            self._reset_backoff(host)
            return self._done(source, url, host, "not_modified", sent_at, t0, status=304, headers=resp_headers)
        if status != 200:
            return self._done(source, url, host, "http_error", sent_at, t0, status=status, headers=resp_headers,
                              error=f"HTTP {status}")
        if len(raw) > cap:
            return self._done(source, url, host, "too_large", sent_at, t0, status=status, headers=resp_headers,
                              error=f"body over {cap} bytes")
        try:
            body = decompress(raw, resp_headers.get("content-encoding", ""), cap)
        except TooLarge:
            return self._done(source, url, host, "too_large", sent_at, t0, status=status, headers=resp_headers,
                              error=f"decompressed body over {cap} bytes")
        except (zlib.error, EOFError, OSError) as exc:
            return self._done(source, url, host, "network", sent_at, t0, status=status, headers=resp_headers,
                              error=f"bad encoding ({type(exc).__name__})")
        self._reset_backoff(host)
        etag, modified = _header(resp_headers, "etag"), _header(resp_headers, "last-modified")
        if etag or modified:
            self._validators[url] = (etag, modified)
        return self._done(source, url, host, "ok", sent_at, t0, status=status, headers=resp_headers, body=body)

    # ------------------------------------------------------------------ results

    def _done(self, source: str, url: str, host: str, result: str, started: datetime, t0: float, *,
              status: int | None = None, headers: Mapping[str, str] | None = None, body: bytes | None = None,
              error: str | None = None) -> FetchResult:
        ms = int((_time.perf_counter() - t0) * 1000)
        size = len(body) if body is not None else 0
        level = logging.INFO if result in ("ok", "not_modified") else logging.WARNING
        self.log.log(level, "fetch %s %s -> %s status=%s bytes=%d ms=%d", source, host, result, status, size, ms)
        return self._result(source, url, result, started, status=status, headers=headers, body=body, error=error)

    def _result(self, source: str, url: str, result: str, started: datetime, *, status: int | None = None,
                headers: Mapping[str, str] | None = None, body: bytes | None = None,
                error: str | None = None) -> FetchResult:
        return FetchResult(source=source, url=url, result=result, status=status, body=body,
                           headers=dict(headers or {}), started_at=iso(started), ended_at=iso(self.clock.now()),
                           bytes=len(body) if body is not None else 0, not_modified=result == "not_modified",
                           error=error)


def build_fetcher(ctx: Any) -> HttpFetcher:
    """AppContext service (DESIGN 2.1): the one fetcher of a Scout run; its per-host limits are
    kept in `state/http_hosts.json` across restarts."""
    log = ctx.log.getChild("http")
    log.info("TLS: certificates verified with %s", "the OS trust store (truststore)" if TLS_TRUST == "os_trust_store"
             else "Python's default context (truststore not importable)")
    state = HostStateFile(ctx.paths, ctx.paths.state / FILE_NAME, log)
    return HttpFetcher(ctx.cfg, ctx.clock, log=log, state=state)
