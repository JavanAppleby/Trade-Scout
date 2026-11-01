"""scout.http (SCOUT_SPEC 6.1, 12; DESIGN 10): hosts, https, spacing, caps, UA, conditional GET,
backoff, size cap, decoding, and that fetch never raises. No network: FakeTransport + ManualClock."""

from __future__ import annotations

import gzip
import io
import logging
import socket
import urllib.error
import zlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import TEST_EMAIL
from helpers_fakes import FakeResponse, FakeTransport
from helpers_wire import wire_config

from scout import http as H
from scout.clock import ManualClock
from scout.config import config_from_dict
from scout.config_load import ConfigError
from scout.http import HttpFetcher, RawResponse, build_fetcher, decompress, urllib_transport
from scout.http_state import HostStateFile
from scout.paths import ScoutPaths

PRN = "https://www.prnewswire.com/rss/news-releases-list.rss"
TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
HALTS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NOW_CAL = "https://nowflation.com/data/release_calendar.json"


def fetcher(cfg, clock, responses=None):
    t = FakeTransport(responses or {})
    return HttpFetcher(cfg, clock, transport=t), t


# --------------------------------------------------------------------------- refusals


def test_empty_contact_email_means_no_http_and_no_transport_call(scout_dir, clock):
    cfg = config_from_dict({}, scout_dir)
    f, t = fetcher(cfg, clock, {PRN: FakeResponse(200, {}, b"x")})
    got = f.fetch("prnewswire", PRN)
    assert got.result == "no_http" and got.body is None and t.calls == []


def test_default_transport_is_never_reached_without_email(scout_dir, clock, no_network):
    got = HttpFetcher(config_from_dict({}, scout_dir), clock).fetch("prnewswire", PRN)
    assert got.result == "no_http" and no_network.attempts == []


@pytest.mark.parametrize("source, url, why", [
    ("prnewswire", "http://www.prnewswire.com/rss/news-releases-list.rss", "https"),
    ("prnewswire", "https://evil.example.com/rss", "not allowed"),
    ("prnewswire", "https://www.sec.gov/files/company_tickers_exchange.json", "not allowed"),
    ("nope", PRN, "unknown source"),
    ("prnewswire", "https://www.prnewswire.com:8443/rss", "port"),
    ("prnewswire", "https://user:pw@www.prnewswire.com/rss", "credentials"),
])
def test_refused_urls_never_reach_the_transport(cfg, clock, source, url, why):
    f, t = fetcher(cfg, clock, {url: FakeResponse(200, {}, b"x")})
    got = f.fetch(source, url)
    assert got.result == "refused" and why in got.error and t.calls == []


def test_disabled_source_is_refused(scout_dir, clock):
    cfg = wire_config(scout_dir, sources={"prnewswire": {"enabled": False}})
    f, t = fetcher(cfg, clock, {PRN: FakeResponse(200, {}, b"x")})
    assert f.fetch("prnewswire", PRN).result == "refused" and t.calls == []


# --------------------------------------------------------------------------- headers and logging


def test_user_agent_and_encoding_headers(cfg, clock):
    seen = {}

    def transport(url, headers, timeout_s, max_bytes):
        seen.update(headers=dict(headers), timeout=timeout_s, max_bytes=max_bytes)
        return RawResponse(200, {}, b"ok")

    got = HttpFetcher(cfg, clock, transport=transport).fetch("prnewswire", PRN)
    assert got.ok and got.body == b"ok" and got.status == 200
    assert seen["headers"]["User-Agent"] == cfg.user_agent() and TEST_EMAIL in seen["headers"]["User-Agent"]
    assert seen["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert seen["timeout"] == cfg.fetch_timeout_s and seen["max_bytes"] == cfg.fetch_max_bytes + 1


def test_contact_email_never_logged_nor_in_repr(cfg, clock, caplog):
    caplog.set_level(logging.DEBUG)
    f, _ = fetcher(cfg, clock, {PRN: [FakeResponse(403, {}, b""), FakeResponse(200, {}, b"x")]})
    f.fetch("prnewswire", PRN)
    f.fetch("prnewswire", "https://www.prnewswire.com/other")
    assert caplog.records and TEST_EMAIL not in caplog.text and "User-Agent" not in caplog.text
    assert TEST_EMAIL not in repr(f)


# --------------------------------------------------------------------------- spacing and caps


def test_sec_hosts_spaced_at_least_half_a_second(cfg, clock):
    f, t = fetcher(cfg, clock, {TICKERS: FakeResponse(200, {}, b"{}")})
    times = []
    for _ in range(3):
        f.fetch("edgar_tickers", TICKERS)
        times.append(clock.monotonic())
    assert len(t.calls) == 3
    assert all(b - a >= 0.5 - 1e-9 for a, b in zip(times, times[1:]))


def test_nasdaqtrader_hard_minimum_beats_config(scout_dir, clock):
    cfg = wire_config(scout_dir, host_min_spacing_s={"www.nasdaqtrader.com": 1.0})
    f, _ = fetcher(cfg, clock, {HALTS: FakeResponse(200, {}, b"<rss/>")})
    assert f.spacing_for("www.nasdaqtrader.com") == 60.0
    f.fetch("nasdaq_halts", HALTS)
    t0 = clock.monotonic()
    f.fetch("nasdaq_halts", HALTS)
    assert clock.monotonic() - t0 >= 60.0 - 1e-9


def test_default_spacing_and_configured_spacing(scout_dir, clock):
    cfg = wire_config(scout_dir, http_default_host_spacing_s=2.5, host_min_spacing_s={"www.prnewswire.com": 47.0})
    f, _ = fetcher(cfg, clock)
    assert f.spacing_for("cdn-api.cboe.com") == 2.5  # the Cboe VIX host: no configured spacing
    assert f.spacing_for("www.prnewswire.com") == 47.0
    assert f.spacing_for("data.sec.gov") == 2.5  # configured above the hard minimum 0.5


def test_no_wait_when_spacing_already_passed(cfg, clock):
    f, _ = fetcher(cfg, clock, {PRN: FakeResponse(200, {}, b"x")})
    f.fetch("prnewswire", PRN)
    clock.advance(61)
    t0 = clock.monotonic()
    f.fetch("prnewswire", PRN)
    assert clock.monotonic() == t0


def test_host_daily_cap_then_reset_next_et_day(cfg, clock):
    f, t = fetcher(cfg, clock, {NOW_CAL: FakeResponse(200, {}, b"{}")})
    results = [f.fetch("nowflation", NOW_CAL).result for _ in range(11)]
    assert results[:10] == ["ok"] * 10 and results[10] == "host_cap" and len(t.calls) == 10
    clock.advance(24 * 3600)
    assert f.fetch("nowflation", NOW_CAL).result == "ok"


# --------------------------------------------------------------------------- statuses


def test_conditional_get_sends_validators_and_304_is_not_modified(cfg, clock):
    f, t = fetcher(cfg, clock, {PRN: [FakeResponse(200, {"ETag": '"abc"', "Last-Modified": "Mon"}, b"x"),
                                      FakeResponse(304, {}, b"")]})
    assert f.fetch("prnewswire", PRN).ok
    assert "If-None-Match" not in t.calls[0][1]
    got = f.fetch("prnewswire", PRN)
    assert {"If-None-Match", "If-Modified-Since"} <= set(t.calls[1][1])
    assert got.result == "not_modified" and got.not_modified and got.body is None
    f.fetch("prnewswire", PRN, conditional=False)
    assert "If-None-Match" not in t.calls[2][1]


@pytest.mark.parametrize("status", [403, 429])
def test_backoff_doubles_to_cap_and_resets_on_success(scout_dir, clock, status):
    cfg = wire_config(scout_dir, source_backoff_max_min=5)
    f, t = fetcher(cfg, clock, {PRN: [FakeResponse(status, {}, b""), FakeResponse(status, {}, b""),
                                      FakeResponse(status, {}, b""), FakeResponse(200, {}, b"x")]})
    first = f.fetch("prnewswire", PRN)
    assert first.result == "http_error" and first.status == status and f.backoff_for("www.prnewswire.com") == 120
    blocked = f.fetch("prnewswire", PRN)
    assert blocked.result == "backoff" and len(t.calls) == 1  # no request while backing off
    clock.advance(120)
    f.fetch("prnewswire", PRN)
    assert f.backoff_for("www.prnewswire.com") == 240
    clock.advance(240)
    f.fetch("prnewswire", PRN)
    assert f.backoff_for("www.prnewswire.com") == 300  # capped at 5 minutes
    clock.advance(300)
    assert f.fetch("prnewswire", PRN).ok and f.backoff_for("www.prnewswire.com") == 0
    assert len(t.calls) == 4  # never a retry storm


@pytest.mark.parametrize("status", [500, 404, 301, 302])
def test_other_statuses_are_http_errors_without_backoff(cfg, clock, status):
    f, _ = fetcher(cfg, clock, {PRN: FakeResponse(status, {"Location": "https://evil.example.com"}, b"")})
    got = f.fetch("prnewswire", PRN)
    assert got.result == "http_error" and got.status == status and f.backoff_for("www.prnewswire.com") == 0


@pytest.mark.parametrize("exc, result", [
    (TimeoutError("t"), "timeout"), (socket.timeout("t"), "timeout"),
    (urllib.error.URLError(TimeoutError("t")), "timeout"), (urllib.error.URLError("refused"), "network"),
    (ConnectionResetError("r"), "network"), (OSError("x"), "network"), (ValueError("odd"), "network"),
    (RuntimeError("odd"), "network"),
])
def test_failures_map_to_results_and_never_raise(cfg, clock, exc, result):
    f, _ = fetcher(cfg, clock, {PRN: exc})
    got = f.fetch("prnewswire", PRN)
    assert got.result == result and got.body is None and got.started_at and got.ended_at


def test_transport_returning_garbage_never_raises(cfg, clock):
    f = HttpFetcher(cfg, clock, transport=lambda *a: object())
    assert f.fetch("prnewswire", PRN).result == "http_error"


# --------------------------------------------------------------------------- size and decoding


def test_body_over_cap_is_too_large(scout_dir, clock):
    cfg = wire_config(scout_dir, fetch_max_bytes=1000)
    f, _ = fetcher(cfg, clock, {PRN: FakeResponse(200, {}, b"x" * 1001)})
    assert f.fetch("prnewswire", PRN).result == "too_large"
    f2, t2 = fetcher(cfg, clock, {PRN: FakeResponse(200, {}, b"x" * 600)})
    assert f2.fetch("prnewswire", PRN, max_bytes=500).result == "too_large" and t2.calls[0][3] == 501


def test_max_bytes_never_above_config(scout_dir, clock):
    cfg = wire_config(scout_dir, fetch_max_bytes=1000)
    f, t = fetcher(cfg, clock, {PRN: FakeResponse(200, {}, b"x")})
    f.fetch("prnewswire", PRN, max_bytes=10**9)
    assert t.calls[0][3] == 1001


def test_gzip_and_deflate_are_decoded(cfg, clock):
    body = b"<rss>" + b"a" * 500 + b"</rss>"
    raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = raw_deflate.compress(body) + raw_deflate.flush()
    cases = [({"Content-Encoding": "gzip"}, gzip.compress(body)), ({}, gzip.compress(body)),
             ({"content-encoding": "deflate"}, zlib.compress(body)), ({"Content-Encoding": "deflate"}, raw)]
    for headers, wire in cases:
        f, _ = fetcher(cfg, ManualClock(clock.now()), {PRN: FakeResponse(200, headers, wire)})
        got = f.fetch("prnewswire", PRN)
        assert got.ok and got.body == body and got.bytes == len(body)


def test_decompression_bomb_is_too_large(scout_dir, clock):
    cfg = wire_config(scout_dir, fetch_max_bytes=10_000)
    bomb = gzip.compress(b"\0" * 5_000_000)
    assert len(bomb) < 10_000
    f, _ = fetcher(cfg, clock, {PRN: FakeResponse(200, {"Content-Encoding": "gzip"}, bomb)})
    assert f.fetch("prnewswire", PRN).result == "too_large"
    with pytest.raises(H.TooLarge):
        decompress(bomb, "gzip", 100)


def test_corrupt_gzip_is_a_network_result(cfg, clock):
    f, _ = fetcher(cfg, clock, {PRN: FakeResponse(200, {"Content-Encoding": "gzip"}, b"\x1f\x8bnot gzip")})
    assert f.fetch("prnewswire", PRN).result == "network"


# --------------------------------------------------------------------------- urllib_transport (no network)


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers=None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = SimpleNamespace(items=lambda: list((headers or {}).items()))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_urllib_transport_reads_capped_and_lowercases_headers(monkeypatch):
    seen = {}

    def fake_open(request, timeout):
        seen.update(ua=request.get_header("User-agent"), timeout=timeout, url=request.full_url)
        return _Resp(b"0123456789", headers={"Content-Encoding": "gzip"})

    monkeypatch.setattr(H._OPENER, "open", fake_open)
    got = urllib_transport(PRN, {"User-Agent": "Scout/0.1.0 (x@y.z)"}, 7.0, 4)
    assert got.status == 200 and got.body == b"0123" and got.headers == {"content-encoding": "gzip"}
    assert seen == {"ua": "Scout/0.1.0 (x@y.z)", "timeout": 7.0, "url": PRN}


def test_urllib_transport_http_error_comes_back_as_status(monkeypatch):
    def fake_open(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {"X": "1"}, io.BytesIO(b"no"))

    monkeypatch.setattr(H._OPENER, "open", fake_open)
    got = urllib_transport(PRN, {}, 5.0, 100)
    assert got.status == 403 and got.body == b"no"


def test_redirects_are_never_followed():
    handler = H._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/") is None


def test_build_fetcher_from_context(cfg, clock):
    ctx = SimpleNamespace(cfg=cfg, clock=clock, log=logging.getLogger("scout"), paths=ScoutPaths.from_config(cfg))
    f = build_fetcher(ctx)
    assert isinstance(f, HttpFetcher) and f.transport is urllib_transport
    assert f._state is not None and f._state.path == ctx.paths.state / "http_hosts.json"


def test_started_and_ended_times_are_utc_iso(cfg, clock):
    f, _ = fetcher(cfg, clock, {PRN: FakeResponse(200, {}, b"x")})
    got = f.fetch("prnewswire", PRN)
    assert got.started_at.endswith("Z") and got.ended_at.endswith("Z")
    assert f.requests_today("www.prnewswire.com", clock.now()) == 1
    assert f.requests_today("www.prnewswire.com", clock.now() + timedelta(days=1)) == 0


# --------------------------------------------------------------------------- limits kept across restarts


def _persisted(cfg, clock, responses):
    paths = ScoutPaths.from_config(cfg)
    state = HostStateFile(paths, paths.state / "http_hosts.json")
    t = FakeTransport(responses)
    return HttpFetcher(cfg, clock, transport=t, state=state), t


def test_daily_cap_survives_a_restart(cfg, clock):
    """Regression: the count lived in memory, so each restart gave nowflation.com a fresh 10."""
    f, t = _persisted(cfg, clock, {NOW_CAL: FakeResponse(200, {}, b"{}")})
    assert [f.fetch("nowflation", NOW_CAL).result for _ in range(7)] == ["ok"] * 7
    for _restart in range(2):
        f, t = _persisted(cfg, clock, {NOW_CAL: FakeResponse(200, {}, b"{}")})
    results = [f.fetch("nowflation", NOW_CAL).result for _ in range(5)]
    assert results == ["ok"] * 3 + ["host_cap"] * 2 and len(t.calls) == 3
    clock.advance(24 * 3600)
    f, _ = _persisted(cfg, clock, {NOW_CAL: FakeResponse(200, {}, b"{}")})
    assert f.fetch("nowflation", NOW_CAL).result == "ok"  # a new ET day


def test_spacing_survives_a_restart(cfg, clock):
    """Regression: a restart within 60 s of the last halts poll sent the next one at once."""
    f, _ = _persisted(cfg, clock, {HALTS: FakeResponse(200, {}, b"x")})
    f.fetch("nasdaq_halts", HALTS)
    clock.advance(20)
    f, _ = _persisted(cfg, clock, {HALTS: FakeResponse(200, {}, b"x")})
    assert f.last_request_at("www.nasdaqtrader.com") == clock.now() - timedelta(seconds=20)
    assert f.source_last_request_at("nasdaq_halts") == clock.now() - timedelta(seconds=20)
    t0 = clock.monotonic()
    f.fetch("nasdaq_halts", HALTS)
    assert clock.monotonic() - t0 == pytest.approx(40)  # waited out the 60 s hard minimum


def test_backoff_survives_a_restart(scout_dir, clock):
    cfg = wire_config(scout_dir, source_backoff_max_min=5)
    f, _ = _persisted(cfg, clock, {PRN: FakeResponse(429, {}, b"")})
    assert f.fetch("prnewswire", PRN).result == "http_error"
    f, t = _persisted(cfg, clock, {PRN: FakeResponse(200, {}, b"x")})
    assert f.fetch("prnewswire", PRN).result == "backoff" and t.calls == []
    assert f.backoff_for("www.prnewswire.com") == 120


def test_a_broken_state_file_starts_empty_and_never_raises(cfg, clock):
    paths = ScoutPaths.from_config(cfg)
    paths.state.mkdir(parents=True, exist_ok=True)
    (paths.state / "http_hosts.json").write_text("{not json", encoding="utf-8")
    f, _ = _persisted(cfg, clock, {PRN: FakeResponse(200, {}, b"x")})
    assert f.fetch("prnewswire", PRN).ok


def test_nowflation_hard_cap_is_in_code(scout_dir, clock):
    """Regression: the cap was only config. It could be raised, removed by a table that left the
    host out, or dodged with a www. alias."""
    with pytest.raises(ConfigError, match="hard cap"):
        wire_config(scout_dir, host_max_requests_per_day={"nowflation.com": 500})
    with pytest.raises(ConfigError, match="hard cap"):
        wire_config(scout_dir, host_max_requests_per_day={"www.nowflation.com": 11})
    cfg = wire_config(scout_dir, host_max_requests_per_day={"www.sec.gov": 1000})  # nowflation left out
    f, t = fetcher(cfg, clock, {NOW_CAL: FakeResponse(200, {}, b"{}")})
    assert f.cap_for("nowflation.com") == 10 and f.cap_for("www.nowflation.com") == 10
    assert f.cap_for("NOWFLATION.COM") == 10 and f.cap_for("evilnowflation.com") is None
    results = [f.fetch("nowflation", NOW_CAL).result for _ in range(11)]
    assert results[-1] == "host_cap" and len(t.calls) == 10
    assert f.requests_today("www.nowflation.com", clock.now()) == 10  # one shared count
    lower = wire_config(scout_dir, host_max_requests_per_day={"nowflation.com": 4})
    assert fetcher(lower, clock)[0].cap_for("nowflation.com") == 4


def test_a_clock_stepped_back_never_sleeps_for_hours(cfg, clock):
    """Regression: with the wall clock stepped back 3 h, the next request to the host slept spacing +
    3 h inside the main loop (STOP unchecked, no source polled)."""
    f, t = fetcher(cfg, clock, {HALTS: FakeResponse(200, {}, b"x")})
    f.fetch("nasdaq_halts", HALTS)
    clock.set(clock.now() - timedelta(hours=3))
    slept = []
    clock.sleep = slept.append  # type: ignore[method-assign]
    assert f.fetch("nasdaq_halts", HALTS).ok and len(t.calls) == 2
    assert all(s <= 60.0 for s in slept)


def test_a_future_time_in_the_state_file_is_clamped_on_load(cfg, clock):
    """Regression: a last_request saved in the future survived a restart unchanged."""
    f, _ = _persisted(cfg, clock, {HALTS: FakeResponse(200, {}, b"x")})
    f.fetch("nasdaq_halts", HALTS)
    clock.set(clock.now() - timedelta(hours=3))
    f, t = _persisted(cfg, clock, {HALTS: FakeResponse(200, {}, b"x")})
    assert f.last_request_at("www.nasdaqtrader.com") == clock.now()
    assert f.source_last_request_at("nasdaq_halts") == clock.now()
    t0 = clock.monotonic()
    f.fetch("nasdaq_halts", HALTS)
    assert clock.monotonic() - t0 == pytest.approx(60)  # the spacing, not 3 h more


def test_wire_hosts_have_a_hard_minimum_in_code(scout_dir, clock):
    """Regression: www.prnewswire.com and www.globenewswire.com fell back to the 1 s default, so a
    config value alone could poll a wire every few seconds (SS 12: at least 30 s, in code)."""
    cfg = wire_config(scout_dir, host_min_spacing_s={"www.prnewswire.com": 1.0})
    f, _ = fetcher(cfg, clock)
    assert f.spacing_for("www.prnewswire.com") == 30.0 and f.spacing_for("www.globenewswire.com") == 30.0


# --------------------------------------------------------------------------- TLS (no network)


def test_tls_verifies_with_the_os_trust_store():
    import ssl

    import truststore  # a declared dependency: installed by `pip install -e .[dev]` (and CI)

    ctx, which = H.ssl_context()
    assert which == "os_trust_store" and isinstance(ctx, truststore.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert H.TLS_TRUST == "os_trust_store" and isinstance(H._SSL_CONTEXT, truststore.SSLContext)


def test_the_opener_uses_the_verifying_context():
    import ssl
    import urllib.request

    https = [h for h in H._OPENER.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1 and https[0]._context is H._SSL_CONTEXT
    assert https[0]._context.verify_mode == ssl.CERT_REQUIRED and https[0]._context.check_hostname is True
    assert any(isinstance(h, H._NoRedirect) for h in H._OPENER.handlers)  # redirects still refused


def test_tls_falls_back_to_pythons_default_context(monkeypatch):
    import ssl
    import sys

    monkeypatch.setitem(sys.modules, "truststore", None)  # `import truststore` raises ImportError
    ctx, which = H.ssl_context()
    assert which == "python_default" and type(ctx) is ssl.SSLContext
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_build_fetcher_logs_which_trust_is_used(cfg, clock, caplog):
    ctx = SimpleNamespace(cfg=cfg, clock=clock, log=logging.getLogger("scout"), paths=ScoutPaths.from_config(cfg))
    with caplog.at_level(logging.INFO, logger="scout"):
        build_fetcher(ctx)
    assert any("TLS: certificates verified with the OS trust store" in r.getMessage() for r in caplog.records)


def test_tls_verification_is_never_disabled_in_code():
    import re
    from pathlib import Path

    root = Path(H.__file__).resolve().parent
    bad = re.compile(r"CERT_NONE|CERT_OPTIONAL|check_hostname\s*=\s*False|_create_unverified_context|verify\s*=\s*False")
    hits = [f"{p.name}:{i}" for p in sorted(root.glob("*.py"))
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if bad.search(line)]
    assert hits == []
