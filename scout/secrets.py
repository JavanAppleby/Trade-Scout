"""Secrets: Scout's own .env and redaction (SCOUT_SPEC 2.8, DESIGN 2.1).

`load_env` reads only `.env` in Scout's own folder and sets only the names in ALLOWED_ENV that are
not already set. It never logs or returns a value. Scout never reads any other bot's `.env`, any
`conf\\` folder or any token file. `redact` masks Anthropic keys, any `x-api-key`,
`APCA-API-KEY-ID` or `APCA-API-SECRET-KEY` value, the value of every allowed variable, and e-mail
addresses (the contact email included) in text bound for a log.

The owner's free Alpaca market-data key (0.3.2, the market feed for the reads; SCOUT_SPEC 19.5) is
two lines in the same `.env`: a key id under one of `ALPACA_ID_NAMES` and a secret under one of
`ALPACA_SECRET_NAMES` (the names Alpaca's own tools use, as three pairs: `ALPACA_KEY_PAIRS`).
`alpaca_key()` reads them from the environment only; the first complete pair wins, and only when no
pair is complete do the first id and the first secret found (mixed names) make the key. An
`AlpacaKey` never shows either value in its repr, and only `headers()` hands them to the fetch,
which never logs a request header.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from scout.config import ScoutConfig
    from scout.paths import ScoutPaths

ALPACA_ID_NAMES = ("ALPACA_API_KEY", "APCA_API_KEY_ID", "ALPACA_KEY_ID")
ALPACA_SECRET_NAMES = ("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY", "ALPACA_API_SECRET")
ALPACA_KEY_PAIRS = tuple(zip(ALPACA_ID_NAMES, ALPACA_SECRET_NAMES))  # the three documented pairs, in order
ALLOWED_ENV = ("ANTHROPIC_API_KEY", *ALPACA_ID_NAMES, *ALPACA_SECRET_NAMES)
MASK = "<redacted>"
_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_HEADER = re.compile(r"(?i)((?:x-api-key|apca-api-key-id|apca-api-secret-key)['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

__all__ = ["ALLOWED_ENV", "ALPACA_ID_NAMES", "ALPACA_KEY_PAIRS", "ALPACA_SECRET_NAMES", "MASK", "AlpacaKey",
           "alpaca_key", "load_env", "redact"]


@dataclass(frozen=True)
class AlpacaKey:
    """The owner's Alpaca market-data key. Its repr never shows either half."""

    key_id: str = field(repr=False)
    secret: str = field(repr=False)

    def __repr__(self) -> str:
        return f"AlpacaKey({MASK})"

    __str__ = __repr__

    def headers(self) -> dict[str, str]:
        """The two request headers Alpaca's market-data service reads (never logged by the fetch)."""
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret}


def _first_set(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def alpaca_key() -> AlpacaKey | None:
    """The Alpaca key from the environment, or None unless both halves are found. The first COMPLETE
    pair of `ALPACA_KEY_PAIRS` wins, so an id and a secret of different pairs are never mixed while a
    whole pair is there. Only when no pair is complete do the first id name and the first secret name
    found make the key (the owner's two lines may use mixed names)."""
    for id_name, secret_name in ALPACA_KEY_PAIRS:
        key_id, secret = _first_set((id_name,)), _first_set((secret_name,))
        if key_id is not None and secret is not None:
            return AlpacaKey(key_id, secret)
    key_id, secret = _first_set(ALPACA_ID_NAMES), _first_set(ALPACA_SECRET_NAMES)
    if key_id is None or secret is None:
        return None
    return AlpacaKey(key_id, secret)


def load_env(paths: ScoutPaths) -> list[str]:
    """Set allowed variables from Scout's .env (never overriding the environment). Returns the
    names set (never the values). A missing or unreadable file sets nothing."""
    try:
        text = paths.env_file.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    done: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if name in ALLOWED_ENV and value and not os.environ.get(name):
            os.environ[name] = value
            done.append(name)
    return done


def redact(text: str, cfg: ScoutConfig | None = None) -> str:
    """`text` with keys, x-api-key values and e-mail addresses masked."""
    out = _KEY.sub(MASK, str(text))
    out = _HEADER.sub(lambda m: m.group(1) + MASK, out)
    if cfg is not None and cfg.contact_email:
        out = out.replace(cfg.contact_email, MASK)
    out = _EMAIL.sub(MASK, out)
    for name in ALLOWED_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            out = out.replace(value, MASK)
    return out
