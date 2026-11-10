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


# ------------------------------------------------------------------ the Alpaca key (0.3.2; SS 2.8, 19.5)

FAKE_ID = "made-up-id-in-test-secrets"  # made up and not shaped like a real key, so no scanner mistakes it
FAKE_SECRET = "made-up-secret-in-test-secrets-01"


def test_load_env_sets_alpaca_names_and_redact_masks_them(paths: ScoutPaths) -> None:
    from scout.secrets import alpaca_key  # noqa: PLC0415

    paths.env_file.write_text(f"APCA_API_KEY_ID={FAKE_ID}\nAPCA_API_SECRET_KEY='{FAKE_SECRET}'\nOTHER=1\n",
                              encoding="utf-8")
    assert load_env(paths) == ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"]
    key = alpaca_key()
    assert key is not None and key.headers() == {"APCA-API-KEY-ID": FAKE_ID, "APCA-API-SECRET-KEY": FAKE_SECRET}
    for shown in (repr(key), str(key), f"{key}", repr([key])):
        assert FAKE_ID not in shown and FAKE_SECRET not in shown
    out = redact(f"id {FAKE_ID} secret {FAKE_SECRET}")
    assert FAKE_ID not in out and FAKE_SECRET not in out


@pytest.mark.parametrize("id_name", ["ALPACA_API_KEY", "APCA_API_KEY_ID", "ALPACA_KEY_ID"])
@pytest.mark.parametrize("secret_name", ["ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY", "ALPACA_API_SECRET"])
def test_every_accepted_key_line_name(paths: ScoutPaths, id_name: str, secret_name: str) -> None:
    from scout.secrets import alpaca_key  # noqa: PLC0415

    paths.env_file.write_text(f"{id_name}={FAKE_ID}\n", encoding="utf-8")
    load_env(paths)
    assert alpaca_key() is None  # only one half: off
    paths.env_file.write_text(f"{id_name}={FAKE_ID}\n{secret_name}={FAKE_SECRET}\n", encoding="utf-8")
    load_env(paths)
    key = alpaca_key()
    assert key is not None and key.key_id == FAKE_ID and key.secret == FAKE_SECRET


@pytest.mark.parametrize("outer, pair", [
    ({"ALPACA_API_KEY": "OUTERID0000000000001"}, ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")),
    ({"ALPACA_SECRET_KEY": "outerSecret000000000000000000000000001"}, ("ALPACA_KEY_ID", "ALPACA_API_SECRET")),
    ({"APCA_API_KEY_ID": "OUTERID0000000000002", "ALPACA_API_SECRET": "outerSecret00000000000000000000000002"},
     ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")),
])
def test_the_first_complete_pair_wins_over_stray_halves(paths: ScoutPaths, monkeypatch: pytest.MonkeyPatch,
                                                        outer: dict[str, str], pair: tuple[str, str]) -> None:
    """Review secrets F2: a stray half from outside Scout's .env (another tool's) is never mixed with the
    owner's complete pair (made-up values)."""
    from scout.secrets import ALPACA_KEY_PAIRS, alpaca_key  # noqa: PLC0415

    assert ALPACA_KEY_PAIRS == (("ALPACA_API_KEY", "ALPACA_SECRET_KEY"), ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
                                ("ALPACA_KEY_ID", "ALPACA_API_SECRET"))
    for name, value in outer.items():
        monkeypatch.setenv(name, value)
    paths.env_file.write_text(f"{pair[0]}={FAKE_ID}\n{pair[1]}={FAKE_SECRET}\n", encoding="utf-8")
    load_env(paths)
    key = alpaca_key()
    assert key is not None and (key.key_id, key.secret) == (FAKE_ID, FAKE_SECRET)


def test_two_complete_pairs_the_first_documented_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.secrets import alpaca_key  # noqa: PLC0415

    monkeypatch.setenv("ALPACA_KEY_ID", "THIRDID0000000000003")
    monkeypatch.setenv("ALPACA_API_SECRET", "thirdSecret00000000000000000000000003")
    monkeypatch.setenv("APCA_API_KEY_ID", FAKE_ID)
    monkeypatch.setenv("APCA_API_SECRET_KEY", FAKE_SECRET)
    key = alpaca_key()
    assert key is not None and (key.key_id, key.secret) == (FAKE_ID, FAKE_SECRET)


def test_mixed_names_still_work_when_no_pair_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's two lines may use names of different pairs: with no complete pair, the first id name
    and the first secret name found make the key."""
    from scout.secrets import alpaca_key  # noqa: PLC0415

    monkeypatch.setenv("APCA_API_KEY_ID", FAKE_ID)
    monkeypatch.setenv("ALPACA_API_SECRET", FAKE_SECRET)
    key = alpaca_key()
    assert key is not None and (key.key_id, key.secret) == (FAKE_ID, FAKE_SECRET)


def test_only_a_secret_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.secrets import alpaca_key  # noqa: PLC0415

    monkeypatch.setenv("ALPACA_SECRET_KEY", FAKE_SECRET)
    monkeypatch.setenv("APCA_API_KEY_ID", "   ")
    assert alpaca_key() is None


def test_redact_masks_apca_headers() -> None:
    text = ("{'APCA-API-KEY-ID': 'abc123', 'apca-api-secret-key': \"def456\"} APCA-API-KEY-ID: ghi789 "
            "Apca-Api-Secret-Key=jkl012")
    out = redact(text)
    for value in ("abc123", "def456", "ghi789", "jkl012"):
        assert value not in out
